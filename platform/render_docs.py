#!/usr/bin/env python3
"""
extract_mermaid.py — 将 md 中所有 mermaid 块渲染为 PNG 并替换
用法: python3 extract_mermaid.py <input.md> [output.md]
依赖: npm install -g @mermaid-js/mermaid-cli
"""
import re
import subprocess
import hashlib
import sys
from pathlib import Path

ASSETS_DIR = "assets"

def render_mermaid(code: str, out_dir: Path) -> Path:
    digest = hashlib.md5(code.encode()).hexdigest()[:8]
    mmd_file = out_dir / f"{digest}.mmd"
    png_file = out_dir / f"{digest}.png"

    if png_file.exists():          # 相同图表不重复渲染
        return png_file

    mmd_file.write_text(code)
    result = subprocess.run(
        ["mmdc", "-i", str(mmd_file), "-o", str(png_file),
        "-b", "white", "-w", "1600", "--scale", "2",
        "-p", "/tmp/puppeteer-config.json"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"mmdc 渲染失败:\n{result.stderr}")

    mmd_file.unlink()              # 清理临时 .mmd 文件
    return png_file

def process(md_path: str, out_path: str | None = None) -> None:
    src = Path(md_path)
    dst = Path(out_path) if out_path else src.with_stem(src.stem + "_rendered")
    out_dir = Path(ASSETS_DIR)
    out_dir.mkdir(exist_ok=True)

    content = src.read_text(encoding="utf-8")
    counter = [0]

    def replace_block(m: re.Match) -> str:
        counter[0] += 1
        code = m.group(1).strip()
        print(f"  渲染第 {counter[0]} 个 mermaid 块...", end=" ")
        png = render_mermaid(code, out_dir)
        print("✓")
        return f"![]({png})"

    result = re.sub(r"```mermaid\n(.*?)```", replace_block,
                    content, flags=re.DOTALL)

    dst.write_text(result, encoding="utf-8")
    print(f"\n共替换 {counter[0]} 个块，输出: {dst}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 extract_mermaid.py <input.md> [output.md]")
        sys.exit(1)
    process(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)