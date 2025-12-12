#!/usr/bin/env python3
"""
Template generator script for Home Assistant configuration.

This script processes template files in the templates/ directory:
- Reads values.yaml from each subfolder
- Uses Mustache templating engine to template filenames and content
- Generates output files with templated values
"""

import os
import re
import json
from pathlib import Path
from typing import Dict, List, Any

try:
    import chevron
except ImportError:
    print("Error: chevron package is required. Install it with: pip install chevron")
    exit(1)


def load_values(values_path: Path) -> Dict[str, List[str]]:
    """Load values.yaml and return as dictionary.
    
    Parses simple YAML format: key: ["value1", "value2"]
    """
    result = {}
    with open(values_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # Match pattern: key: ["value1", "value2"] or key: [value1, value2]
            match = re.match(r'^(\w+):\s*(.+)$', line)
            if match:
                key = match.group(1)
                value_str = match.group(2).strip()
                # Parse the array (handles both quoted and unquoted strings)
                try:
                    # Use JSON parser for the array part
                    ids = json.loads(value_str)
                    if isinstance(ids, list):
                        result[key] = [str(id) for id in ids]
                except json.JSONDecodeError:
                    # Fallback: try to extract values manually
                    ids_match = re.findall(r'["\']?([^,"\']+)["\']?', value_str)
                    result[key] = ids_match
    return result


def template_content(content: str, context: Dict[str, Any]) -> str:
    """Template the content using Mustache with custom delimiters to avoid conflicts with Home Assistant templates.
    
    Uses [[ and ]] as delimiters to avoid conflicts with Home Assistant's {{ and {% syntax.
    """
    # Use custom delimiters [[ and ]] to avoid conflicts with Home Assistant templates
    return chevron.render(content, context, def_ldel='[[', def_rdel=']]')


def template_filename(filename: str, context: Dict[str, Any]) -> str:
    """Template the filename - handle both old <% var %> and new [[var]] syntax."""
    result = filename
    # Replace Mustache syntax in filenames (using [[ ]] delimiters)
    result = result.replace('[[room]]', context.get('room', ''))
    # Also handle old syntax for backward compatibility
    result = result.replace('<% room %>', context.get('room', ''))
    return result


def process_template_file(template_path: Path, output_base: Path, context: Dict[str, Any], template_dir: Path):
    """Process a single template file and generate output."""
    # Calculate relative path from template directory
    relative_path = template_path.relative_to(template_dir)
    
    # Template the filename
    templated_filename = template_filename(relative_path.name, context)
    
    # Read template content
    with open(template_path, 'r', encoding='utf-8') as f:
        template_content_str = f.read()
    
    # Template the content
    templated_content = template_content(template_content_str, context)
    
    # Build output path in root directory
    # Remove the template folder name (e.g., "lights") from the path
    # So templates/lights/entities/... becomes entities/... in root
    path_parts = list(relative_path.parent.parts)
    # Remove the first part which is the template folder name
    if path_parts and path_parts[0] == template_dir.name:
        path_parts = path_parts[1:]
    
    # Template directory names if they contain variables
    templated_parts = [template_filename(part, context) for part in path_parts]
    
    output_dir = output_base / Path(*templated_parts) if templated_parts else output_base
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_path = output_dir / templated_filename
    
    # Write the templated file
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(templated_content)
    
    print(f"Generated: {output_path}")


def process_template_folder(template_folder: Path, output_base: Path):
    """Process all template files in a folder."""
    values_path = template_folder / 'values.yaml'
    
    if not values_path.exists():
        print(f"Warning: No values.yaml found in {template_folder}, skipping...")
        return
    
    # Load values
    values = load_values(values_path)
    
    if not values:
        print(f"Warning: values.yaml in {template_folder} is empty, skipping...")
        return
    
    print(f"\nProcessing template folder: {template_folder.name}")
    print(f"Found {len(values)} rooms: {', '.join(values.keys())}")
    
    # Find all YAML files (excluding values.yaml)
    yaml_files = []
    for root, dirs, files in os.walk(template_folder):
        for file in files:
            if file.endswith('.yaml') and file != 'values.yaml':
                yaml_files.append(Path(root) / file)
    
    # Process each room
    for room, ids in values.items():
        print(f"\n  Processing room: {room} (ids: {ids})")
        
        # Create Mustache context
        # Convert ids list to list of objects for Mustache iteration
        lights = [{'id': str(light_id)} for light_id in ids]
        context = {
            'room': room,
            'lights': lights
        }
        
        # Process each template file
        for template_file in yaml_files:
            process_template_file(template_file, output_base, context, template_folder)


def main():
    """Main function to process all template folders."""
    script_dir = Path(__file__).parent
    templates_dir = script_dir / 'templates'
    output_dir = script_dir  # Output to root directory instead of generated/
    
    if not templates_dir.exists():
        print(f"Error: templates/ directory not found at {templates_dir}")
        return
    
    # Process each subfolder in templates/
    template_folders = [f for f in templates_dir.iterdir() if f.is_dir()]
    
    if not template_folders:
        print(f"No subfolders found in {templates_dir}")
        return
    
    print(f"Found {len(template_folders)} template folder(s)")
    
    for template_folder in template_folders:
        process_template_folder(template_folder, output_dir)
    
    print("\n✓ Template generation complete!")


if __name__ == '__main__':
    main()
