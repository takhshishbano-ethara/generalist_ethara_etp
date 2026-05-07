import re
import logging

_logger = logging.getLogger(__name__)


def parse_rubric_text(text):
    if not text or not text.strip():
        return []

    lines = [l.strip() for l in text.strip().split('\n')]
    lines = [l for l in lines if l]

    categories = []
    current_category = None
    current_dimension = None
    i = 0

    while i < len(lines):
        line = lines[i]

        if line == '*':
            i += 1
            continue

        options = _try_parse_options_line(line)
        if options and current_dimension is not None:
            current_dimension['options'] = options
            i += 1
            continue

        if current_dimension is not None and not current_dimension.get('description'):
            if '?' in line or line.lower().startswith('which') or (len(line) > 40 and not _looks_like_title(line)):
                current_dimension['description'] = line
                i += 1
                continue

        if current_category is not None and _looks_like_title(line):
            current_dimension = {'name': line, 'description': '', 'options': []}
            current_category['dimensions'].append(current_dimension)
            i += 1
            continue

        if current_category is None:
            current_category = {'name': line, 'dimensions': []}
            categories.append(current_category)
        elif not current_category.get('dimensions'):
            current_dimension = {'name': line, 'description': '', 'options': []}
            current_category['dimensions'].append(current_dimension)

        i += 1

    return categories


def _looks_like_title(line):
    if len(line) > 60:
        return False
    if line.endswith('?') or line.endswith('.') or line.endswith(','):
        return False
    if line.startswith('(') and line.endswith(')'):
        return False
    return True


def _try_parse_options_line(line):
    prefer_pattern = re.findall(
        r'((?:Strongly|Slightly)\s+Prefer\s+[A-Z]|Tie)',
        line
    )
    if len(prefer_pattern) >= 3:
        return prefer_pattern

    known_patterns = [
        r'(Strongly Prefer [A-Z]|Slightly Prefer [A-Z]|Tie)',
        r'(\d+\s*[-–]\s*[A-Za-z ]+)',
    ]
    for pattern in known_patterns:
        matches = re.findall(pattern, line)
        if len(matches) >= 3:
            return matches

    return None


def match_and_create_rubric(env, project, parsed_categories):
    Dimension = env['rubric.dimension'].sudo()
    Option = env['rubric.category.option'].sudo()
    Category = env['rubric.category'].sudo()

    result = {'dimensions': [], 'options': []}

    existing_categories = {
        cat.name.strip().lower(): cat
        for cat in project.rubric_category_ids
    }

    first_category = project.rubric_category_ids[:1]

    for parsed_cat in parsed_categories:
        cat_name = parsed_cat['name'].strip()
        cat_key = cat_name.lower()

        if cat_key in existing_categories:
            category = existing_categories[cat_key]
        elif first_category:
            category = first_category
        else:
            category = Category.create({
                'name': cat_name,
                'project_id': project.id,
            })
            project.is_rubrics_required = True
            existing_categories[cat_key] = category
            first_category = category

        existing_dimensions = {
            dim.name.strip().lower(): dim
            for dim in category.dimension_ids
        }
        existing_options = {
            opt.name.strip().lower(): opt
            for opt in category.option_ids
        }

        for parsed_dim in parsed_cat.get('dimensions', []):
            dim_name = parsed_dim['name'].strip()
            dim_key = dim_name.lower()
            dim_desc = parsed_dim.get('description', '')

            if dim_key in existing_dimensions:
                dimension = existing_dimensions[dim_key]
                result['dimensions'].append({
                    'id': dimension.id, 'name': dimension.name,
                    'is_new': False, 'is_temp': dimension.is_temp,
                })
            else:
                dimension = Dimension.create({
                    'name': dim_name,
                    'description': dim_desc,
                    'category_id': category.id,
                    'is_temp': True,
                    'is_required': False,
                })
                existing_dimensions[dim_key] = dimension
                result['dimensions'].append({
                    'id': dimension.id, 'name': dimension.name,
                    'is_new': True, 'is_temp': True,
                })

            for opt_name in parsed_dim.get('options', []):
                opt_name_clean = opt_name.strip()
                opt_key = opt_name_clean.lower()

                if opt_key in existing_options:
                    option = existing_options[opt_key]
                    result['options'].append({
                        'id': option.id, 'name': option.name,
                        'is_new': False, 'is_temp': option.is_temp,
                    })
                else:
                    next_seq = (len(existing_options) + 1) * 10
                    option = Option.create({
                        'name': opt_name_clean,
                        'category_id': category.id,
                        'sequence': next_seq,
                        'is_temp': True,
                    })
                    existing_options[opt_key] = option
                    result['options'].append({
                        'id': option.id, 'name': option.name,
                        'is_new': True, 'is_temp': True,
                    })

    return result
