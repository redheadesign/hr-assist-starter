"""Create a plain, readable PDF from the confirmed local profile."""
import argparse
from html import escape
from pathlib import Path
from workspace import local_path, read_json


def create(font, bold, output):
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, KeepTogether
    except ImportError as exc:
        raise RuntimeError("Install requirements-cv.txt before creating a PDF") from exc
    profile = read_json(local_path("config/profile.json"), {})
    if not profile.get("name") or not profile.get("headline"):
        raise ValueError("Confirm the name and headline before creating a CV")
    pdfmetrics.registerFont(TTFont("CVRegular", str(Path(font).resolve(strict=True))))
    pdfmetrics.registerFont(TTFont("CVBold", str(Path(bold).resolve(strict=True))))
    pdfmetrics.registerFontFamily("CVRegular", normal="CVRegular", bold="CVBold")
    styles = {
        "body": ParagraphStyle("body", fontName="CVRegular", fontSize=10, leading=14, spaceAfter=5, textColor=colors.HexColor("#222222")),
        "name": ParagraphStyle("name", fontName="CVBold", fontSize=21, leading=25, spaceAfter=7),
        "section": ParagraphStyle("section", fontName="CVBold", fontSize=11, leading=15, spaceBefore=12, spaceAfter=6, keepWithNext=True),
    }
    story = []
    def p(text, style="body"):
        return Paragraph(escape(str(text)).replace("\n", "<br/>"), styles[style])
    story.extend([p(profile["name"], "name"), p(profile["headline"])])
    contacts = [profile.get("location")] + list(profile.get("contacts", {}).values()) + list(profile.get("links", {}).values())
    story.extend(p(value) for value in contacts if value)
    if profile.get("summary"):
        story.extend([Spacer(1, 7), p(profile["summary"])])
    if profile.get("experience"):
        story.append(p("Опыт работы", "section"))
        for item in profile["experience"]:
            header = f"{item.get('company', '')} | {item.get('role', '')}"
            period = item.get("period") or " – ".join(str(item.get(k) or "") for k in ("start", "end"))
            story.append(KeepTogether([p(header, "section"), p(period)]))
            for bullet in item.get("bullets", []):
                story.append(p("• " + str(bullet)))
    for key, title in (("skills", "Навыки"), ("languages", "Языки"), ("education", "Образование")):
        if profile.get(key):
            story.append(p(title, "section"))
            for item in profile[key]:
                if isinstance(item, dict):
                    # Explicit fields only; never leak an internal source/notes object.
                    item = ", ".join(str(item[k]) for k in ("institution", "program", "period") if item.get(k))
                story.append(p(item))
    target = local_path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(str(target), pagesize=A4, leftMargin=42, rightMargin=42, topMargin=38, bottomMargin=38, title="CV", author="").build(story)
    print(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font", required=True, help="TTF supporting the profile language")
    parser.add_argument("--font-bold", required=True)
    parser.add_argument("--output", default="artifacts/cv.pdf", help="Path relative to .local")
    args = parser.parse_args()
    create(args.font, args.font_bold, args.output)
