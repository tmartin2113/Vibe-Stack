"""
Skill Registry Search Mixin

Provides skill search, matching, and confidence scoring logic
for the SkillRegistry. Extracted for maintainability.

All methods access SkillRegistry attributes via ``self`` (index,
tier dirs, config thresholds, etc.), so this must be mixed into
a class that provides those attributes.
"""

import math
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SkillRegistrySearchMixin:
    """
    Mixin providing skill search and matching across all tiers.

    Expects the composing class to provide:
    - self.index: dict with tiers structure
    - self.official_dir, self.local_dir, self.temp_dir: Path
    - self.OFFICIAL_CONFIDENCE_THRESHOLD, LOCAL_CONFIDENCE_THRESHOLD, TEMP_CONFIDENCE_THRESHOLD
    - self._workspace_skills: dict
    - self._local_indexed_sources: list
    - self._enable_remote: bool
    - self._skills_config: SkillsConfig
    - self._find_remote_skill(): from SkillRegistryRemoteMixin
    - self._load_local_index(): from SkillRegistry
    - self.register_skill(): from SkillRegistry
    - self.security: SkillSecurity
    """

    def _get_embedding_cache(self):
        """
        Return the lazily-initialized SkillEmbeddingCache.

        The import is local so ``skill_registry`` stays loadable in
        environments where the embedder module (or its network-touching
        ``requests`` dependency) is absent — e.g., test collection,
        ``--doctor`` mode, or CI runners without vLLM.
        """
        cache = getattr(self, "_embedding_cache", None)
        if cache is None:
            from .skill_embeddings import SkillEmbeddingCache
            cache = SkillEmbeddingCache(self.base_dir)
            self._embedding_cache = cache
        return cache

    def _query_vec_for(self, requirement: str) -> Optional[List[float]]:
        """
        Embed a search query, memoizing on ``self`` so a single
        ``find_skills()`` call that iterates multiple tiers only pays
        the embedding round-trip once.
        """
        last = getattr(self, "_last_query_vec", None)
        if last is not None and last[0] == requirement:
            return last[1]
        try:
            vec = self._get_embedding_cache().embed_query(requirement)
        except Exception as e:  # pragma: no cover — defensive
            logger.debug(f"Query embedding failed: {e}")
            vec = None
        self._last_query_vec = (requirement, vec)
        return vec

    def find_skill(self, requirement: str) -> Tuple[str, Optional[str], Optional[Path]]:
        """
        Find best matching skill across all tiers.

        Search order:
        1. Official (local cache) — fast, no network
        2. Local persistent skills — fast, no network
        3. Retained temp skills — ephemeral skills cached from prior sessions
        4. Remote sources in priority order — fetches on demand, caches result
        5. Ephemeral generation — fallback when nothing matches

        Args:
            requirement: Natural language description of what's needed

        Returns:
            Tuple of (tier, skill_name, skill_path)
            - If tier is "ephemeral", skill_name and skill_path are None
        """
        # Tier 0: Workspace (project-specific, highest priority, in-memory)
        ws_match = self._search_workspace(requirement)
        if ws_match and ws_match["confidence"] >= self.LOCAL_CONFIDENCE_THRESHOLD:
            skill_name = ws_match["name"]
            logger.info(f"🗂️  Found workspace skill: {skill_name} (confidence: {ws_match['confidence']:.2f})")
            return ("workspace", skill_name, None)

        # Tier 1: Check official skills (already cached locally)
        official_match = self._search_tier(requirement, "official")
        if official_match and official_match["confidence"] >= self.OFFICIAL_CONFIDENCE_THRESHOLD:
            skill_name = official_match["name"]
            skill_path = self.official_dir / skill_name
            logger.info(f"📚 Found official skill: {skill_name} (confidence: {official_match['confidence']:.2f})")
            return ("official", skill_name, skill_path)

        # Tier 2: Check local persistent skills
        local_match = self._search_tier(requirement, "local")
        if local_match and local_match["confidence"] >= self.LOCAL_CONFIDENCE_THRESHOLD:
            skill_name = local_match["name"]
            skill_path = self.local_dir / skill_name
            logger.info(f"🏠 Found local skill: {skill_name} (confidence: {local_match['confidence']:.2f})")
            return ("local", skill_name, skill_path)

        # Tier 2.5: Check retained temp skills from previous sessions
        temp_match = self._search_tier(requirement, "temp")
        if temp_match and temp_match["confidence"] >= self.TEMP_CONFIDENCE_THRESHOLD:
            skill_name = temp_match["name"]
            skill_path = self.temp_dir / skill_name
            logger.info(f"♻️ Found retained temp skill: {skill_name} (confidence: {temp_match['confidence']:.2f})")
            return ("temp", skill_name, skill_path)

        # Tier 2.75: Local indexed sources (e.g. openclaw — searched by index, fetched on demand)
        for local_source in self._local_indexed_sources:
            result = self._search_local_indexed_source(requirement, local_source)
            if result:
                skill_name, skill_path = result
                return ("temp", skill_name, skill_path)

        # Tier 3: Probe remote sources in priority order
        if self._enable_remote:
            for source in self._skills_config.sources:
                if not source.enabled:
                    continue
                remote_result = self._find_remote_skill(requirement, source)
                if remote_result:
                    skill_name, skill_path = remote_result
                    logger.info(
                        f"🌐 Fetched skill from {source.name}: {skill_name}"
                    )
                    return ("official", skill_name, skill_path)

        # Tier 4: Need to generate ephemeral skill
        logger.info(f"🔧 No existing skill found, will generate ephemeral skill")
        return ("ephemeral", None, None)

    def find_skills(
        self,
        requirement: str,
        max_skills: int = 3,
        min_confidence: float = 0.35,
    ) -> List[Tuple[str, Optional[str], Optional[Path]]]:
        """
        Find multiple matching skills across all tiers.

        Like ``find_skill()`` but returns up to *max_skills* matches above
        *min_confidence*, sorted by quality-weighted score.  This lets the
        ``SkillLoaderNode`` apply progressive disclosure (primary skill gets
        70 % context budget, secondary skills get summaries).

        Remote source probing is still included: any remote match is appended
        if the local tiers don't fill the quota.

        Args:
            requirement: Natural language description of what's needed.
            max_skills:  Maximum number of skills to return.
            min_confidence: Floor confidence for inclusion.

        Returns:
            List of ``(tier, skill_name, skill_path)`` tuples, ordered by
            descending confidence.  Empty list means "generate ephemeral".
        """
        results: List[Tuple[float, str, str, Optional[Path]]] = []  # (score, tier, name, path)

        # Tier 0: workspace skills (project-specific, highest priority)
        ws_threshold = max(self.LOCAL_CONFIDENCE_THRESHOLD, min_confidence)
        for match in self._search_workspace_all(requirement, ws_threshold):
            # Small tiebreaker so workspace beats same-confidence persistent skills
            results.append((min(match["confidence"] + 0.05, 1.0), "workspace", match["name"], None))

        tier_dirs = {
            "official": self.official_dir,
            "local": self.local_dir,
            "temp": self.temp_dir,
        }
        thresholds = {
            "official": self.OFFICIAL_CONFIDENCE_THRESHOLD,
            "local": self.LOCAL_CONFIDENCE_THRESHOLD,
            "temp": self.TEMP_CONFIDENCE_THRESHOLD,
        }

        for tier_name, tier_dir in tier_dirs.items():
            threshold = max(thresholds[tier_name], min_confidence)
            for match in self._search_tier_all(requirement, tier_name, threshold):
                results.append((
                    match["confidence"],
                    tier_name,
                    match["name"],
                    tier_dir / match["name"],
                ))

        # Local indexed sources — append if quota not filled
        if len(results) < max_skills and self._local_indexed_sources:
            seen_names = {r[2] for r in results}
            for local_source in self._local_indexed_sources:
                if len(results) >= max_skills:
                    break
                result = self._search_local_indexed_source(
                    requirement, local_source, min_confidence
                )
                if result:
                    skill_name, skill_path = result
                    if skill_name not in seen_names:
                        results.append((0.4, "temp", skill_name, skill_path))
                        seen_names.add(skill_name)

        # Remote sources — append if quota not filled
        if self._enable_remote and len(results) < max_skills:
            seen_names = {r[2] for r in results}
            for source in self._skills_config.sources:
                if not source.enabled:
                    continue
                remote_result = self._find_remote_skill(requirement, source)
                if remote_result:
                    skill_name, skill_path = remote_result
                    if skill_name not in seen_names:
                        results.append((0.5, "official", skill_name, skill_path))
                        seen_names.add(skill_name)
                if len(results) >= max_skills:
                    break

        # Sort by score descending, take top N
        results.sort(key=lambda r: r[0], reverse=True)
        top = results[:max_skills]

        if top:
            for score, tier, name, _ in top:
                logger.info(
                    f"🔍 find_skills match: {name} (tier={tier}, confidence={score:.2f})"
                )

        return [(tier, name, path) for _, tier, name, path in top]

    def _search_workspace(self, requirement: str) -> Optional[Dict[str, Any]]:
        """Return the best workspace skill match, or None."""
        matches = self._search_workspace_all(requirement)
        return matches[0] if matches else None

    def _search_workspace_all(
        self,
        requirement: str,
        min_confidence: float = 0.35,
    ) -> List[Dict[str, Any]]:
        """Return all workspace skill matches above min_confidence, sorted by confidence."""
        if not self._workspace_skills:
            return []

        req_lower = requirement.lower()
        query_vec = self._query_vec_for(requirement)
        cache = self._get_embedding_cache() if query_vec is not None else None

        matches = []
        for skill_name, skill_data in self._workspace_skills.items():
            # Workspace skills are in-memory only (never persisted), so
            # embeddings are computed on the fly when the embedder is up.
            sem_score: Optional[float] = None
            if query_vec is not None and cache is not None:
                vec = cache.embed_skill(
                    skill_name,
                    skill_data.get("description", ""),
                    skill_data.get("task_types", []),
                )
                if vec is not None:
                    sem_score = cache.semantic_score(query_vec, skill_name)
            confidence = self._calculate_match_confidence(
                req_lower,
                skill_data.get("description", "").lower(),
                skill_data.get("task_types", []),
                semantic_score=sem_score,
            )
            if confidence >= min_confidence:
                matches.append({
                    "name": skill_name,
                    "confidence": min(confidence, 1.0),
                    "data": skill_data,
                })
        matches.sort(key=lambda m: m["confidence"], reverse=True)
        return matches

    def _search_local_indexed_source(
        self,
        requirement: str,
        source: Dict[str, Any],
        min_confidence: float = 0.35,
    ) -> Optional[Tuple[str, Path]]:
        """
        Search one local indexed source for the best match.

        If a match is found above min_confidence, copies the skill to
        temp/ and registers it so subsequent tasks find it without
        re-scanning.

        Returns:
            (skill_name, skill_path) tuple on success, or None.
        """
        index = self._load_local_index(source)
        if not index:
            return None

        req_lower = requirement.lower()
        best_name: Optional[str] = None
        best_score: float = 0.0

        for skill_name, skill_data in index.items():
            desc = skill_data.get("description", "")
            tags = skill_data.get("tags", [])
            confidence = self._calculate_match_confidence(req_lower, desc.lower(), tags)
            if confidence > best_score:
                best_score = confidence
                best_name = skill_name

        if not best_name or best_score < min_confidence:
            return None

        # Load and register the matched skill into temp/ for reuse
        skill_src = source["skills_path"] / best_name
        if not skill_src.is_dir() or not (skill_src / "SKILL.md").exists():
            return None

        logger.info(
            f"🔍 Local source '{source['name']}': matched '{best_name}' "
            f"(confidence: {best_score:.2f})"
        )

        # If already in temp, return it directly
        if best_name in self.index["tiers"]["temp"]["skills"]:
            return (best_name, self.temp_dir / best_name)

        # Copy to temp/ and register
        dest = self.temp_dir / best_name
        try:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_src, dest)
        except OSError as exc:
            logger.warning(f"Failed to copy '{best_name}' to temp/: {exc}")
            return None

        try:
            self.register_skill(
                name=best_name,
                description=index[best_name].get("description", best_name),
                tier="temp",
                task_types=index[best_name].get("tags", []),
                skill_path=dest,
            )
        except Exception as exc:
            logger.warning(f"Failed to register '{best_name}' from local source: {exc}")
            shutil.rmtree(dest, ignore_errors=True)
            return None

        return (best_name, dest)

    def _search_tier_all(
        self,
        requirement: str,
        tier: str,
        min_confidence: float = 0.35,
    ) -> List[Dict[str, Any]]:
        """
        Return *all* matches above *min_confidence* in a single tier.

        Same scoring logic as ``_search_tier()`` but collects every match
        rather than only the best one.

        Args:
            requirement: Natural language description.
            tier: ``"official"``, ``"local"``, or ``"temp"``.
            min_confidence: Minimum quality-weighted score.

        Returns:
            List of dicts with ``"name"``, ``"confidence"``, ``"data"``.
        """
        tier_skills = self.index["tiers"][tier]["skills"]
        if not tier_skills:
            return []

        requirement_lower = requirement.lower()
        query_vec = self._query_vec_for(requirement)
        cache = self._get_embedding_cache() if query_vec is not None else None
        matches: List[Dict[str, Any]] = []

        for skill_name, skill_data in tier_skills.items():
            sem_score: Optional[float] = None
            if query_vec is not None and cache is not None:
                # Registered tiers have warm vectors from register_skill();
                # re-embed lazily if the stored description changed.
                cache.embed_skill(
                    skill_name,
                    skill_data.get("description", ""),
                    skill_data.get("task_types", []),
                )
                sem_score = cache.semantic_score(query_vec, skill_name)
            match_confidence = self._calculate_match_confidence(
                requirement_lower,
                skill_data.get("description", "").lower(),
                skill_data.get("task_types", []),
                semantic_score=sem_score,
            )

            avg_score = skill_data.get("avg_score", 0.0)
            usage_count = skill_data.get("usage_count", 0)

            if usage_count > 0 and avg_score > 0:
                quality_factor = avg_score / 100.0
                usage_bonus = min(math.log2(usage_count + 1) / 10.0, 0.1)
            else:
                quality_factor = 0.5
                usage_bonus = 0.0

            weighted_score = (match_confidence * 0.7) + (quality_factor * 0.3) + usage_bonus

            if weighted_score >= min_confidence:
                matches.append({
                    "name": skill_name,
                    "confidence": min(weighted_score, 1.0),
                    "data": skill_data,
                })

        matches.sort(key=lambda m: m["confidence"], reverse=True)
        return matches

    def _search_tier(self, requirement: str, tier: str) -> Optional[Dict[str, Any]]:
        """
        Search for matching skills in a specific tier.

        Uses quality-weighted ranking: keyword match confidence is boosted
        by the skill's historical avg_score and usage_count.  This means
        skills that have performed well in the reinforcement loop are
        preferred over untested or low-scoring alternatives.

        Args:
            requirement: Natural language description
            tier: "official", "local", or "temp"

        Returns:
            Dict with "name" and "confidence" if found, None otherwise
        """
        tier_skills = self.index["tiers"][tier]["skills"]

        if not tier_skills:
            return None

        requirement_lower = requirement.lower()
        query_vec = self._query_vec_for(requirement)
        cache = self._get_embedding_cache() if query_vec is not None else None
        best_match = None
        best_score = 0.0

        for skill_name, skill_data in tier_skills.items():
            sem_score: Optional[float] = None
            if query_vec is not None and cache is not None:
                cache.embed_skill(
                    skill_name,
                    skill_data.get("description", ""),
                    skill_data.get("task_types", []),
                )
                sem_score = cache.semantic_score(query_vec, skill_name)
            match_confidence = self._calculate_match_confidence(
                requirement_lower,
                skill_data.get("description", "").lower(),
                skill_data.get("task_types", []),
                semantic_score=sem_score,
            )

            # Quality-weighted ranking: factor in historical performance.
            # avg_score (0-100) is normalized to 0-1 and blended with
            # match confidence.  Skills with no usage history get a
            # neutral 0.5 quality factor (no penalty, no boost).
            # usage_count provides a small confidence bonus (log-scaled)
            # to prefer battle-tested skills over untested ones.
            avg_score = skill_data.get("avg_score", 0.0)
            usage_count = skill_data.get("usage_count", 0)

            if usage_count > 0 and avg_score > 0:
                quality_factor = avg_score / 100.0
                # Small usage bonus: log2(usage+1)/10, capped at 0.1
                usage_bonus = min(math.log2(usage_count + 1) / 10.0, 0.1)
            else:
                quality_factor = 0.5  # Neutral for untested skills
                usage_bonus = 0.0

            # Blend: 70% match relevance, 30% quality history
            weighted_score = (match_confidence * 0.7) + (quality_factor * 0.3) + usage_bonus

            if weighted_score > best_score:
                best_score = weighted_score
                best_match = {
                    "name": skill_name,
                    "confidence": min(weighted_score, 1.0),
                    "data": skill_data
                }

        return best_match

    def _calculate_match_confidence(
        self,
        requirement: str,
        description: str,
        task_types: List[str],
        semantic_score: Optional[float] = None,
    ) -> float:
        """
        Calculate confidence score for a skill match.

        Uses keyword overlap, task type matching, and substring containment.
        Returns score between 0.0 and 1.0.

        When ``semantic_score`` is provided (a clamped cosine similarity
        between the query and the skill's cached embedding), the return
        value is a weighted blend: ``0.4 * keyword + 0.6 * semantic``.
        A ``None`` or non-positive ``semantic_score`` falls back to pure
        keyword scoring — behavior is byte-identical to the pre-embedding
        implementation when the embedder is unavailable.
        """
        # Early exit: no meaningful input → no match
        if not requirement or not requirement.strip():
            return 0.0

        # Normalize: split on spaces, underscores, hyphens, drop empties
        def tokenize(text: str) -> set:
            return {t for t in re.split(r'[\s_\-/]+', text.lower()) if t}

        req_tokens = tokenize(requirement)
        if not req_tokens:
            return 0.0

        desc_tokens = tokenize(description)
        task_tokens = tokenize(' '.join(task_types))

        # Token overlap
        desc_overlap = len(req_tokens & desc_tokens) / len(req_tokens)
        task_overlap = len(req_tokens & task_tokens) / len(req_tokens)

        # Substring containment bonus: if the requirement appears in
        # the description or task types, boost confidence.
        requirement_clean = requirement.replace("_", " ").replace("-", " ").strip()
        description_clean = description.replace("_", " ").replace("-", " ")
        task_str_clean = " ".join(task_types).replace("_", " ").replace("-", " ").lower()

        substring_bonus = 0.0
        if requirement_clean in description_clean:
            substring_bonus = 0.3
        elif any(tok in description_clean for tok in req_tokens if len(tok) > 3):
            substring_bonus = 0.15
        if requirement_clean in task_str_clean:
            substring_bonus = max(substring_bonus, 0.25)

        # Weighted combination
        confidence = (desc_overlap * 0.5) + (task_overlap * 0.25) + substring_bonus
        keyword_conf = min(confidence, 1.0)

        # Blend with embedding similarity when available. The 0.4/0.6 split
        # lets semantic override weak keyword matches while preserving
        # enough keyword weight that exact-term tests keep their ordering.
        if semantic_score is not None and semantic_score > 0:
            return (keyword_conf * 0.4) + (min(semantic_score, 1.0) * 0.6)
        return keyword_conf
