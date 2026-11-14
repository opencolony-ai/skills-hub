"""Reading Office files without any library: the text in a Word, Excel or PowerPoint file, and for a
presentation what a program can say about it (is it a sound file, how many slides, native charts, notes, shapes
outside the slide). Standard library only; never raises on a damaged file, it reports it.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

OFFICE = {".docx": ("word/",), ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/"), ".pptx": ("ppt/slides/",)}
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
      "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
EMU_SLACK = 12700          # one point: shapes may touch the edge


def _num(name: str) -> int:
    m = re.search(r"(\d+)\.xml$", name)
    return int(m.group(1)) if m else 0


def text_of(f: Path) -> str | None:
    """The text in a file, or None when it cannot be read (and so cannot be checked). Presentations come back
    slide by slide, with a one-line summary of their structure first."""
    suf = f.suffix.lower()
    if suf == ".pptx":
        r = pptx_report(f)
        if not r["opens"]:
            return None
        head = (f"[{r['slides']} slides, {r['charts']} native charts, {r['pictures']} pictures, "
                f"notes on {r['slides_with_notes']} slides]")
        return head + "\n" + "\n".join(f"--- slide {i + 1}\n{t}" for i, t in enumerate(r["text"]))
    if suf in OFFICE:
        try:
            with zipfile.ZipFile(f) as z:
                parts = [z.read(n).decode("utf-8", "replace") for n in z.namelist()
                         if n.endswith(".xml") and n.startswith(OFFICE[suf])]
        except (zipfile.BadZipFile, OSError):
            return None
        return re.sub(r"<[^>]+>", " ", " ".join(parts))
    return None


def pptx_report(f: Path) -> dict:
    """{opens, problems, slides, text (per slide), charts, pictures, slides_with_notes, out_of_bounds, empty_slides}.
    `problems` are things that make PowerPoint complain or the deck wrong: a missing part, a broken relationship,
    XML that does not parse, shapes outside the slide."""
    out = {"opens": False, "problems": [], "slides": 0, "text": [], "charts": 0, "pictures": 0,
           "slides_with_notes": 0, "out_of_bounds": [], "empty_slides": []}
    try:
        z = zipfile.ZipFile(f)
    except (zipfile.BadZipFile, OSError) as e:
        out["problems"].append(f"not a PowerPoint file: {e}")
        return out
    with z:
        names = set(z.namelist())
        if "ppt/presentation.xml" not in names or "[Content_Types].xml" not in names:
            out["problems"].append("missing ppt/presentation.xml or [Content_Types].xml")
            return out
        out["opens"] = True
        trees: dict[str, ET.Element] = {}
        for n in sorted(names):
            if n.endswith((".xml", ".rels")):
                try:
                    trees[n] = ET.fromstring(z.read(n))
                except ET.ParseError as e:
                    out["problems"].append(f"{n} is not valid XML ({e})")
        for n, root in trees.items():            # every relationship points at a part that exists
            if n.endswith(".rels"):
                base = PurePosixPath(n).parent.parent
                for rel in root:
                    if rel.get("TargetMode") == "External":
                        continue
                    t = rel.get("Target", "")
                    path = str(PurePosixPath(t[1:] if t.startswith("/") else base / t))
                    parts: list[str] = []
                    for seg in path.split("/"):
                        if seg == "..":
                            if parts:
                                parts.pop()
                        elif seg and seg != ".":
                            parts.append(seg)
                    if "/".join(parts) not in names:
                        out["problems"].append(f"{n}: points at {t}, which is not in the file")
        pres = trees.get("ppt/presentation.xml")
        size = pres.find("p:sldSz", NS) if pres is not None else None
        width, height = (int(size.get("cx")), int(size.get("cy"))) if size is not None else (9144000, 5143500)
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_num)
        out["slides"] = len(slides)
        out["charts"] = len([n for n in names if re.fullmatch(r"ppt/charts/chart\d+\.xml", n)])
        for i, n in enumerate(slides):
            root = trees.get(n)
            if root is None:
                out["text"].append("")
                continue
            text = [" ".join(t.text or "" for t in p.iter(f"{{{NS['a']}}}t")).strip() for p in root.iter(f"{{{NS['a']}}}p")]
            text = "\n".join(t for t in text if t)
            out["text"].append(text)
            pics = len(root.findall(".//p:pic", NS))
            out["pictures"] += pics
            has_chart = bool(root.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/chart}chart"))
            if not text and not pics and not has_chart:
                out["empty_slides"].append(i + 1)
            for sp in root.iter():
                if sp.tag.split("}")[-1] not in ("sp", "pic", "graphicFrame", "cxnSp"):
                    continue
                xfrm = sp.find(".//a:xfrm", NS)
                if xfrm is None:
                    xfrm = sp.find(".//p:xfrm", NS)
                off, ext = (xfrm.find("a:off", NS), xfrm.find("a:ext", NS)) if xfrm is not None else (None, None)
                if off is None or ext is None:
                    continue
                x, y, w, h = int(off.get("x", 0)), int(off.get("y", 0)), int(ext.get("cx", 0)), int(ext.get("cy", 0))
                if x < -EMU_SLACK or y < -EMU_SLACK or x + w > width + EMU_SLACK or y + h > height + EMU_SLACK:
                    out["out_of_bounds"].append(i + 1)
                    break
            rels = trees.get(f"ppt/slides/_rels/{PurePosixPath(n).name}.rels")
            if rels is not None and any("notesSlide" in (r.get("Type") or "") for r in rels):
                out["slides_with_notes"] += 1
    if out["out_of_bounds"]:
        out["problems"].append("shapes outside the slide on slide(s) " + ", ".join(map(str, sorted(set(out["out_of_bounds"])))))
    return out


# ------------------------------------------------------------------ pictures of the slides (when LibreOffice and a PDF renderer exist)

SOFFICE_PLACES = ["/Applications/LibreOffice.app/Contents/MacOS/soffice", "/usr/lib/libreoffice/program/soffice",
                  "/opt/libreoffice/program/soffice", r"C:\Program Files\LibreOffice\program\soffice.exe",
                  r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"]


def find_soffice() -> str:
    """LibreOffice's program: on PATH, else where its installers put it (a Mac app is not on PATH)."""
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    return next((p for p in SOFFICE_PLACES if Path(p).is_file()), "")


def pdf_renderer() -> str:
    """How a PDF page becomes a picture here: "pdftoppm" if it is installed, else "pypdfium2" (a Python package, no
    system program), else ""."""
    if shutil.which("pdftoppm"):
        return "pdftoppm"
    try:
        import PIL  # noqa: F401
        import pypdfium2  # noqa: F401
        return "pypdfium2"
    except ImportError:
        return ""


def have(cmd: str) -> bool:
    """Is this program (or what stands in for it) available: soffice is looked for where it is installed, and
    pdftoppm is met by pypdfium2."""
    if cmd in ("soffice", "libreoffice"):
        return bool(find_soffice())
    if cmd == "pdftoppm":
        return bool(pdf_renderer())
    return bool(shutil.which(cmd))


def render(f: Path, out_dir: Path, dpi: int = 50, timeout: int = 180) -> list[Path]:
    """One PNG per slide in out_dir, via LibreOffice (to PDF) and pdftoppm or pypdfium2. [] when either is missing or fails."""
    office, how = find_soffice(), pdf_renderer()
    if not office or not how:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as t:
        try:
            subprocess.run([office, "--headless", f"-env:UserInstallation=file://{Path(t).as_posix()}/profile", "--convert-to", "pdf",
                            "--outdir", t, str(f)], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
            pdf = Path(t) / (f.stem + ".pdf")
            if not pdf.is_file():
                return []
            if how == "pdftoppm":
                subprocess.run([shutil.which("pdftoppm"), "-png", "-r", str(dpi), str(pdf), str(out_dir / "slide")], capture_output=True, timeout=timeout)
            else:
                import pypdfium2
                doc = pypdfium2.PdfDocument(str(pdf))
                width = len(str(len(doc)))
                for i in range(len(doc)):
                    doc[i].render(scale=dpi / 72).to_pil().save(out_dir / f"slide-{i + 1:0{width}d}.png")
                doc.close()
        except (OSError, subprocess.TimeoutExpired, Exception):          # a picture is a nicety: never fail a run for it
            return sorted(out_dir.glob("slide*.png"))
    return sorted(out_dir.glob("slide*.png"))
