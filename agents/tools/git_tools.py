"""
Git Operations Tool

Git repository operations and analysis: blame, history, diffs, branches.
"""

import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# Optional imports with graceful degradation
try:
    import git
    GIT_AVAILABLE = True
except ImportError:
    GIT_AVAILABLE = False


class GitOperationsTool:
    """
    Git repository operations and analysis.

    Features:
    - Git blame (who wrote what)
    - Commit history analysis
    - Diff parsing
    - Branch information
    - File history
    """

    def __init__(self):
        self.name = "git_operations"
        self.description = "Analyze git repository: blame, history, diffs, branches."
        self.git_available = GIT_AVAILABLE

    def execute(
        self,
        operation: str,
        path: str = ".",
        **kwargs
    ) -> Dict[str, Any]:
        """
        Execute git operation.

        Args:
            operation: "blame", "history", "diff", "status", "branches"
            path: Repository or file path
            **kwargs: Operation-specific arguments

        Returns:
            Dictionary with operation results
        """
        try:
            if operation == "blame":
                return self._git_blame(path, kwargs.get('line_range'))
            elif operation == "history":
                return self._git_history(path, kwargs.get('max_commits', 10))
            elif operation == "diff":
                return self._git_diff(path, kwargs.get('commit1'), kwargs.get('commit2'))
            elif operation == "status":
                return self._git_status(path)
            elif operation == "branches":
                return self._git_branches(path)
            else:
                return {
                    "success": False,
                    "error": f"Unknown operation: {operation}. Use: blame, history, diff, status, branches"
                }

        except Exception as e:
            return {
                "success": False,
                "error": f"Git operation failed: {str(e)}"
            }

    def _git_blame(self, file_path: str, line_range: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
        """Get git blame for a file"""
        if not Path(file_path).is_file():
            return {"success": False, "error": "Not a file"}

        cmd = ['git', 'blame', '--line-porcelain', file_path]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        # Parse blame output
        blame_data = []
        current: Dict[str, Any] = {}

        for line in result.stdout.splitlines():
            if line.startswith('author '):
                current['author'] = line[7:]
            elif line.startswith('author-time '):
                current['timestamp'] = int(line[12:])
            elif line.startswith('summary '):
                current['message'] = line[8:]
            elif line.startswith('\t'):
                if current:
                    current['code'] = line[1:]
                    blame_data.append(current.copy())
                    current = {}

        # Filter by line range if specified
        if line_range:
            start, end = line_range
            blame_data = blame_data[start-1:end]

        return {
            "success": True,
            "file": file_path,
            "total_lines": len(blame_data),
            "lines": blame_data[:100]  # Limit to 100 lines
        }

    def _git_history(self, path: str, max_commits: int) -> Dict[str, Any]:
        """Get commit history"""
        cmd = [
            'git', 'log',
            f'-{max_commits}',
            '--pretty=format:%H|%an|%ae|%at|%s',
            '--', path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        commits = []
        for line in result.stdout.splitlines():
            if '|' in line:
                hash_val, author, email, timestamp, message = line.split('|', 4)
                commits.append({
                    "hash": hash_val[:8],
                    "author": author,
                    "email": email,
                    "timestamp": int(timestamp),
                    "message": message
                })

        return {
            "success": True,
            "path": path,
            "commits_found": len(commits),
            "commits": commits
        }

    def _git_diff(self, path: str, commit1: Optional[str], commit2: Optional[str]) -> Dict[str, Any]:
        """Get git diff"""
        cmd = ['git', 'diff']

        if commit1:
            cmd.append(commit1)
        if commit2:
            cmd.append(commit2)

        if path != '.':
            cmd.extend(['--', path])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        # Parse diff stats
        stats_result = subprocess.run(
            cmd + ['--stat'],
            capture_output=True,
            text=True,
            timeout=10
        )

        return {
            "success": True,
            "diff": result.stdout,
            "stats": stats_result.stdout if stats_result.returncode == 0 else None,
            "has_changes": bool(result.stdout.strip())
        }

    def _git_status(self, path: str) -> Dict[str, Any]:
        """Get git status"""
        result = subprocess.run(
            ['git', 'status', '--porcelain', path],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        # Parse status
        modified = []
        added = []
        deleted = []
        untracked = []

        for line in result.stdout.splitlines():
            status = line[:2]
            file_path = line[3:]

            if 'M' in status:
                modified.append(file_path)
            elif 'A' in status:
                added.append(file_path)
            elif 'D' in status:
                deleted.append(file_path)
            elif '?' in status:
                untracked.append(file_path)

        return {
            "success": True,
            "path": path,
            "modified": modified,
            "added": added,
            "deleted": deleted,
            "untracked": untracked,
            "clean": not (modified or added or deleted or untracked)
        }

    def _git_branches(self, path: str) -> Dict[str, Any]:
        """Get git branches"""
        result = subprocess.run(
            ['git', 'branch', '-a'],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=path if Path(path).is_dir() else '.'
        )

        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        branches = []
        current = None

        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('* '):
                current = line[2:]
                branches.append(line[2:])
            else:
                branches.append(line)

        return {
            "success": True,
            "current_branch": current,
            "branches": branches,
            "total_branches": len(branches)
        }
