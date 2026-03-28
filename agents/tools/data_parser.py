"""
Data Parser Tool

Parse and validate structured data in JSON, YAML, XML, CSV, and TOML formats.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


class DataParserTool:
    """
    Parse and validate structured data.

    Supports:
    - JSON
    - YAML (if pyyaml installed)
    - XML
    - CSV
    - TOML
    """

    def __init__(self):
        self.name = "data_parser"
        self.description = "Parse and validate JSON, YAML, XML, CSV, TOML files."

    def execute(
        self,
        data: str,
        format_type: str = "auto",
        validate_schema: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Parse structured data.

        Args:
            data: Data string or file path
            format_type: "json", "yaml", "xml", "csv", "toml", "auto"
            validate_schema: Optional JSON schema to validate against

        Returns:
            Dictionary with parsed data
        """
        try:
            # Check if data is a file path
            if len(data) < 500 and Path(data).is_file():
                with open(data, 'r') as f:
                    data_str = f.read()

                # Auto-detect format from extension
                if format_type == "auto":
                    ext = Path(data).suffix.lower()
                    format_map = {
                        '.json': 'json',
                        '.yaml': 'yaml',
                        '.yml': 'yaml',
                        '.xml': 'xml',
                        '.csv': 'csv',
                        '.toml': 'toml'
                    }
                    format_type = format_map.get(ext, 'json')
            elif len(data) < 500 and (Path(data).suffix or '/' in data or '\\' in data):
                # Looks like a file path but doesn't exist
                return {
                    "success": False,
                    "error": f"File not found: {data}"
                }
            else:
                data_str = data

                # Auto-detect from content
                if format_type == "auto":
                    data_stripped = data_str.strip()
                    if data_stripped.startswith('{') or data_stripped.startswith('['):
                        format_type = 'json'
                    elif data_stripped.startswith('<'):
                        format_type = 'xml'
                    else:
                        format_type = 'yaml'

            # Parse based on format
            if format_type == "json":
                parsed = json.loads(data_str)
            elif format_type == "yaml":
                if not YAML_AVAILABLE:
                    return {
                        "success": False,
                        "error": "YAML support requires pyyaml. Install with: pip install pyyaml"
                    }
                parsed = yaml.safe_load(data_str)
            elif format_type == "csv":
                import csv
                lines = data_str.splitlines()
                reader = csv.DictReader(lines)
                parsed = list(reader)
            elif format_type == "xml":
                import xml.etree.ElementTree as ET
                root = ET.fromstring(data_str)
                parsed = self._xml_to_dict(root)
            elif format_type == "toml":
                try:
                    import tomllib
                except ImportError:
                    import tomli as tomllib
                parsed = tomllib.loads(data_str)
            else:
                return {
                    "success": False,
                    "error": f"Unsupported format: {format_type}"
                }

            # Validate schema if provided
            validation: Optional[Dict[str, Any]] = None
            if validate_schema:
                try:
                    import jsonschema
                    jsonschema.validate(parsed, validate_schema)
                    validation = {"valid": True}
                except ImportError:
                    validation = {"error": "jsonschema not installed"}
                except Exception as e:
                    validation = {"valid": False, "errors": str(e)}

            return {
                "success": True,
                "format": format_type,
                "data": parsed,
                "validation": validation,
                "summary": self._summarize_data(parsed)
            }

        except json.JSONDecodeError as e:
            return {
                "success": False,
                "error": f"JSON parse error: {str(e)}",
                "line": e.lineno,
                "column": e.colno
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Parse error: {str(e)}"
            }

    def _xml_to_dict(self, element) -> Dict[str, Any]:
        """Convert XML element to dictionary"""
        result = {}

        # Add attributes
        if element.attrib:
            result['@attributes'] = element.attrib

        # Add text
        if element.text and element.text.strip():
            result['@text'] = element.text.strip()

        # Add children
        for child in element:
            child_data = self._xml_to_dict(child)
            if child.tag in result:
                if not isinstance(result[child.tag], list):
                    result[child.tag] = [result[child.tag]]
                result[child.tag].append(child_data)
            else:
                result[child.tag] = child_data

        return result

    def _summarize_data(self, data: Any) -> Dict[str, Any]:
        """Generate summary of parsed data"""
        if isinstance(data, dict):
            return {
                "type": "object",
                "keys": list(data.keys())[:20],
                "total_keys": len(data)
            }
        elif isinstance(data, list):
            return {
                "type": "array",
                "length": len(data),
                "item_type": type(data[0]).__name__ if data else "unknown"
            }
        else:
            return {
                "type": type(data).__name__,
                "value": str(data)[:100]
            }
