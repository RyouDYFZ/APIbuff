import re

# Regex patterns for ANSI terminal escape sequences
ANSI_CSI_PATTERN = re.compile(rb'\x1b\[[0-9;?]*[a-zA-Z]')
ANSI_OSC_PATTERN = re.compile(rb'\x1b\][^\x07\x1b]*(\x07|\x1b\\)')
ANSI_CHARSET_PATTERN = re.compile(rb'\x1b\([AB012]')
ANSI_OTHER_PATTERN = re.compile(rb'\x1b[@-Z\\-_]')

def strip_ansi(text_bytes: bytes) -> str:
    """Removes ANSI terminal escape codes from raw terminal byte stream."""
    if not text_bytes:
        return ""
    cleaned = ANSI_CSI_PATTERN.sub(b'', text_bytes)
    cleaned = ANSI_OSC_PATTERN.sub(b'', cleaned)
    cleaned = ANSI_CHARSET_PATTERN.sub(b'', cleaned)
    cleaned = ANSI_OTHER_PATTERN.sub(b'', cleaned)
    return cleaned.decode('utf-8', errors='ignore')

def clean_terminal_decorations(text: str) -> str:
    """
    Strips OpenTUI decorative border lines and status bars if present.
    Leaves normal markdown, code blocks, lists, and prose intact.
    """
    lines = text.splitlines()
    filtered_lines = []
    
    for line in lines:
        stripped = line.strip()
        # Filter pure box borders
        if not stripped:
            filtered_lines.append("")
            continue
        if all(c in "─━│┌┐└┘╭╮╰╯─█▀▄ " for c in stripped):
            continue
        # Remove leading/trailing box chars if line was enclosed in a panel
        if stripped.startswith("│") and stripped.endswith("│"):
            stripped = stripped[1:-1].strip()
        filtered_lines.append(stripped)
        
    return "\n".join(filtered_lines).strip()
