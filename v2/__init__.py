"""Legal Document Review System — V2

A two-agent legal review pipeline that produces DOCX redline output.

V2 fixes from V1:
- Structured action model (KEEP/REPLACE/INSERT/DELETE) instead of blob rewrite
- Paragraph-level mapping between Doc A and Doc B before sentence comparison
- Dedicated table reconciliation engine
- Safe DOCX redline writer that never appends/duplicates
- Validation guards against corruption
"""
