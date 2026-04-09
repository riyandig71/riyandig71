"""Legal Document Review System — V3

V3 adds multi-part document support on top of V2:
- Doc A can be a single DOCX or a folder containing Part 1, Part 2, etc.
- Doc B is always a single DOCX (benchmark).
- Each part is reviewed independently against Doc B.
- Output is one redline DOCX per part.
"""
