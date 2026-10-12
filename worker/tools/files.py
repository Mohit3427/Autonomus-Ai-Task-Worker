"""Workspace file tools. Every path is sandboxed to the run's workspace directory."""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .base import Assessment, Tool, ToolError, ToolResult, schema

MAX_READ_CHARS = 20000
TEXT_SUFFIXES = {".txt", ".csv", ".json", ".md", ".html", ".xml", ".log", ".tsv", ".yaml", ".yml"}


def _safe_path(ctx, rel: str) -> Path:
    root = ctx.workspace.resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ToolError("Path escapes the workspace; use a relative path inside the workspace.")
    return p


class ListFiles(Tool):
    name = "list_files"
    description = "List files in the run workspace (downloads and files you have written)."
    input_schema = schema({})

    async def run(self, ctx) -> ToolResult:
        files = sorted(p for p in ctx.workspace.rglob("*") if p.is_file())
        if not files:
            return ToolResult("Workspace is empty.")
        rows = [f"{p.relative_to(ctx.workspace)}  ({p.stat().st_size} bytes)" for p in files]
        return ToolResult("\n".join(rows))


class ReadFile(Tool):
    name = "read_file"
    description = "Read a workspace file. PDFs are converted to text (page by page); text formats are returned as-is."
    input_schema = schema({"path": {"type": "string", "description": "Path relative to the workspace"}}, ["path"])

    async def run(self, ctx, path: str) -> ToolResult:
        p = _safe_path(ctx, path)
        if not p.is_file():
            available = [str(f.relative_to(ctx.workspace)) for f in ctx.workspace.rglob("*") if f.is_file()]
            raise ToolError(f"No such file: {path}. Workspace files: {available or 'none'}")
        if p.suffix.lower() == ".pdf":
            try:
                reader = PdfReader(str(p))
                pages = [f"--- page {i + 1} ---\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages)]
            except PdfReadError as e:
                raise ToolError(f"Could not parse PDF (file may be corrupt or not a PDF): {e}")
            text = "\n".join(pages)
            if not text.strip():
                raise ToolError("PDF contains no extractable text (it may be a scanned image).")
        elif p.suffix.lower() in TEXT_SUFFIXES:
            text = p.read_text(errors="replace")
        else:
            return ToolResult(f"{path} is a binary file ({p.stat().st_size} bytes); cannot display.")
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + f"\n[... truncated {len(text) - MAX_READ_CHARS} chars]"
        return ToolResult(text, data={"file": path})


class WriteFile(Tool):
    name = "write_file"
    description = "Create or overwrite a text file (e.g. a CSV report) in the workspace."
    input_schema = schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])

    async def assess(self, ctx, args):
        return Assessment("local_write", target=args.get("path", ""))

    async def run(self, ctx, path: str, content: str) -> ToolResult:
        p = _safe_path(ctx, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return ToolResult(f"Wrote {len(content)} chars to {path}.", data={"file": path, "evidence": True})


FILE_TOOLS = [ListFiles, ReadFile, WriteFile]
