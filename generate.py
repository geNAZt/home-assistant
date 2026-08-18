#!/usr/bin/env python3
"""
Template generator script for Home Assistant configuration.
"""

import os
import math  # Neu: math modul importieren
from pathlib import Path
from typing import Dict, List, Any

try:
    from jinja2 import Environment, BaseLoader
except ImportError:
    print("Error: jinja2 package is required. Install it with: pip install jinja2")
    exit(1)

try:
    import yaml
except ImportError:
    print("Error: pyyaml package is required. Install it with: pip install pyyaml")
    exit(1)

# ... (load_values bleibt gleich)

def create_jinja_env() -> Environment:
    """Create Jinja2 environment with custom delimiters and math functions."""
    env = Environment(
        loader=BaseLoader(),
        variable_start_string='[[',
        variable_end_string=']]',
        block_start_string='[[%',
        block_end_string='%]]',
        comment_start_string='[[#',
        comment_end_string='#]]',
        trim_blocks=True,
        lstrip_blocks=True
    )
    
    # Registriere die mathematischen Funktionen für die Generator-Ebene
    # Damit kannst du sie sowohl als Filter (x | atan) als auch als Funktion atan(x) nutzen
    env.filters.update({
        'atan': math.atan,
        'tan': math.tan,
        'cos': math.cos,
        'sin': math.sin,
        'rad2deg': math.degrees,
        'deg2rad': math.radians,
    })
    
    # Falls du sie lieber als globale Funktionen statt Filter nutzt:
    env.globals.update({
        'atan': math.atan,
        'tan': math.tan,
        'rad2deg': math.degrees,
        'deg2rad': math.radians,
    })
    
    return env

def load_values(values_path: Path) -> List[Dict]:
    """Load values.yaml and return as list of dictionaries.
    
    Uses proper YAML parsing to handle the format.
    Calculates window glass area and wattage limit (50W/m2) for cover templates.
    """
    with open(values_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    
    if not data:
        return []
    
    for item in data:
        if isinstance(item, dict) and 'windows' in item:
            for window in item['windows']:
                if isinstance(window, dict) and 'area' in window:
                    height = window['area'].get('height', 0)
                    width = window['area'].get('width', 0)
                    area_m2 = height * width
                    # Calculate wattage limit per window at 50 W / m² of glass
                    limit_w = round(area_m2 * 50.0, 2)
                    window['wattage_limit'] = limit_w
                    window['watt_limit'] = limit_w
                    window['glass_area'] = round(area_m2, 4)
    
    return data


def template_content(content: str, context: Dict[str, Any]) -> str:
    """Template the content using Jinja2 with custom delimiters to avoid conflicts with Home Assistant templates.
    
    Uses [[ and ]] for variables and [[% and %]] for blocks to avoid conflicts with Home Assistant's {{ and {% syntax.
    """
    env = create_jinja_env()
    template = env.from_string(content)
    return template.render(**context)


def template_filename(filename: str, context: Dict[str, Any]) -> str:
    """Template the filename using Jinja2 with <% and %> delimiters for variables only."""
    # For filenames, we only need variable substitution, so use simple string replacement
    # or create a minimal Jinja2 environment
    env = Environment(
        loader=BaseLoader(),
        variable_start_string='<%',
        variable_end_string='%>',
        block_start_string='<%#',  # Different from variable start
        block_end_string='%>',
        comment_start_string='<%!',  # Different from both
        comment_end_string='%>',
        trim_blocks=True,
        lstrip_blocks=True
    )
    template = env.from_string(filename)
    return template.render(**context)


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
    print(f"Found {len(values)} room items")
    
    # Helper structures for global templates
    temp_rooms = []
    cover_entries = []
    covers = []
    for item in values:
        if isinstance(item, dict):
            r = item.get('temp_room', item.get('room'))
            if r and r not in temp_rooms:
                temp_rooms.append(r)
            if 'windows' in item:
                for window in item['windows']:
                    if isinstance(window, dict) and 'cover' in window:
                        cov = window['cover']
                        cov_suffix = cov.split('.')[1] if '.' in cov else cov
                        if cov not in covers:
                            covers.append(cov)
                        cover_entries.append({
                            'cover': cov,
                            'cov_suffix': cov_suffix,
                            'room': r,
                            'watt_limit': window.get('watt_limit', 50.0),
                            'wattage_limit': window.get('wattage_limit', 50.0),
                            'glass_area': window.get('glass_area', 1.0),
                        })

    # Find all YAML files (excluding values.yaml)
    yaml_files = []
    for root, dirs, files in os.walk(template_folder):
        for file in files:
            if file.endswith('.yaml') and file != 'values.yaml':
                yaml_files.append(Path(root) / file)
    
    per_item_files = []
    global_files = []
    
    for template_file in yaml_files:
        relative_path = template_file.relative_to(template_folder)
        if '<%' in relative_path.name or '<%' in str(relative_path):
            per_item_files.append(template_file)
        else:
            global_files.append(template_file)
    
    # Process per-item template files
    for item in values:
        print(f"\n  Processing item: {item.get('room', item)}")
        context = {
            **item,
            'rooms': values,
            'items': values,
            'values': values,
            'temp_rooms': temp_rooms,
            'cover_entries': cover_entries,
            'covers': covers,
        }
        for template_file in per_item_files:
            process_template_file(template_file, output_base, context, template_folder)

    # Process global template files (once with full context)
    if global_files:
        print(f"\n  Processing {len(global_files)} global template file(s)")
        global_context = {
            'rooms': values,
            'items': values,
            'values': values,
            'temp_rooms': temp_rooms,
            'cover_entries': cover_entries,
            'covers': covers,
        }
        for template_file in global_files:
            process_template_file(template_file, output_base, global_context, template_folder)


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
