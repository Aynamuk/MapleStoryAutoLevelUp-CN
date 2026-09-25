import os
import re

pattern = re.compile(r'''f"[^"]*{\s*[^'}"]*"\s*[^'}"]*}[^"]*"''')

def scan_file(filepath):
    with open(filepath, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            if pattern.search(line):
                print(f"{filepath}:{lineno}: {line.strip()}")

def walk_dir(path):
    for root, _, files in os.walk(path):
        for file in files:
            if file.endswith(".py"):
                scan_file(os.path.join(root, file))

walk_dir(os.getcwd())
