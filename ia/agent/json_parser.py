import json

def extract_json_blocks(text: str):
    blocks = []
    buffer = ""
    depth = 0
    in_string = False
    escape = False

    for char in text:
        if char == '"' and not escape:
            in_string = not in_string

        if char == "\\" and not escape:
            escape = True
        else:
            escape = False

        if not in_string:
            if char == "{":
                depth += 1
            if depth > 0:
                buffer += char
            if char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        blocks.append(json.loads(buffer))
                    except Exception:
                        pass
                    buffer = ""
        else:
            if depth > 0:
                buffer += char

    return blocks
