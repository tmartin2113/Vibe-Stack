"""
Codebase Search Tool

Semantic search through codebases with AST-aware function/class
search and grep-like text search with context.
"""

import ast
import re
from pathlib import Path
from typing import Dict, Any, List, Optional


class CodebaseSearchTool:
    """
    Semantic search through codebase.

    Features:
    - Find function/class definitions
    - Search by name pattern
    - AST-based search (understands code structure)
    - Grep-like text search with context
    """

    def __init__(self):
        self.name = "codebase_search"
        self.description = "Search codebase for functions, classes, patterns. Understands code structure."

    def execute(
        self,
        query: str,
        path: str = ".",
        search_type: str = "auto",
        file_pattern: str = "*.py",
        max_results: int = 20
    ) -> Dict[str, Any]:
        """
        Search codebase.

        Args:
            query: What to search for
            path: Directory to search in
            search_type: "function", "class", "text", "auto"
            file_pattern: File pattern (e.g., "*.py", "*.js")
            max_results: Maximum results to return

        Returns:
            Dictionary with search results
        """
        try:
            target_path = Path(path)
            if not target_path.exists():
                return {
                    "success": False,
                    "error": f"Path does not exist: {path}"
                }

            # Auto-detect search type
            if search_type == "auto":
                if query.startswith("def ") or query.startswith("function "):
                    search_type = "function"
                elif query.startswith("class "):
                    search_type = "class"
                else:
                    # Try to determine from query
                    if re.match(r'^[A-Z][a-zA-Z0-9]*$', query):
                        search_type = "class"  # CamelCase = likely class
                    elif re.match(r'^[a-z_][a-z0-9_]*$', query):
                        search_type = "function"  # snake_case = likely function
                    else:
                        search_type = "text"

            # Find matching files
            files = list(target_path.rglob(file_pattern))

            results = []

            if search_type == "function":
                results = self._search_functions(query, files, max_results)
            elif search_type == "class":
                results = self._search_classes(query, files, max_results)
            else:  # text search
                results = self._search_text(query, files, max_results)

            return {
                "success": True,
                "query": query,
                "search_type": search_type,
                "files_searched": len(files),
                "results_found": len(results),
                "results": results
            }

        except (OSError, ValueError, re.error) as e:
            return {
                "success": False,
                "error": f"Search failed: {str(e)}"
            }

    def _search_functions(self, query: str, files: List[Path], max_results: int) -> List[Dict[str, Any]]:
        """Search for function definitions"""
        results = []
        query_lower = query.lower()

        for file in files:
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Parse AST for Python files
                if file.suffix == '.py':
                    try:
                        tree = ast.parse(content)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.FunctionDef):
                                if query_lower in node.name.lower():
                                    results.append({
                                        "type": "function",
                                        "name": node.name,
                                        "file": str(file),
                                        "line": node.lineno,
                                        "args": [arg.arg for arg in node.args.args],
                                        "docstring": ast.get_docstring(node)
                                    })
                    except SyntaxError:
                        pass
                else:
                    # Regex fallback for other languages
                    pattern = r'^[\s]*(function|def|async def)\s+(\w*' + re.escape(query) + r'\w*)\s*\('
                    for i, line in enumerate(content.splitlines(), 1):
                        match = re.search(pattern, line, re.IGNORECASE)
                        if match:
                            results.append({
                                "type": "function",
                                "name": match.group(2),
                                "file": str(file),
                                "line": i,
                                "snippet": line.strip()
                            })

                if len(results) >= max_results:
                    break

            except (OSError, UnicodeDecodeError):
                continue

        return results[:max_results]

    def _search_classes(self, query: str, files: List[Path], max_results: int) -> List[Dict[str, Any]]:
        """Search for class definitions"""
        results = []
        query_lower = query.lower()

        for file in files:
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Parse AST for Python files
                if file.suffix == '.py':
                    try:
                        tree = ast.parse(content)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.ClassDef):
                                if query_lower in node.name.lower():
                                    methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
                                    results.append({
                                        "type": "class",
                                        "name": node.name,
                                        "file": str(file),
                                        "line": node.lineno,
                                        "methods": methods[:10],  # First 10 methods
                                        "docstring": ast.get_docstring(node)
                                    })
                    except SyntaxError:
                        pass
                else:
                    # Regex fallback
                    pattern = r'^[\s]*(class|interface)\s+(\w*' + re.escape(query) + r'\w*)'
                    for i, line in enumerate(content.splitlines(), 1):
                        match = re.search(pattern, line, re.IGNORECASE)
                        if match:
                            results.append({
                                "type": "class",
                                "name": match.group(2),
                                "file": str(file),
                                "line": i,
                                "snippet": line.strip()
                            })

                if len(results) >= max_results:
                    break

            except (OSError, UnicodeDecodeError):
                continue

        return results[:max_results]

    def _search_text(self, query: str, files: List[Path], max_results: int) -> List[Dict[str, Any]]:
        """Search for text with context"""
        results = []

        for file in files:
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()

                for i, line in enumerate(lines):
                    if query.lower() in line.lower():
                        # Get context (2 lines before and after)
                        context_start = max(0, i - 2)
                        context_end = min(len(lines), i + 3)
                        context = ''.join(lines[context_start:context_end])

                        results.append({
                            "type": "text",
                            "file": str(file),
                            "line": i + 1,
                            "match": line.strip(),
                            "context": context.strip()
                        })

                        if len(results) >= max_results:
                            return results

            except (OSError, UnicodeDecodeError):
                continue

        return results[:max_results]
