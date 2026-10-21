# Native browser environment first attempt

The DOM checks passed (122/122), but visual inspection showed missing Korean glyphs because the restored container had no Korean font. This evidence is retained as an environment setup failure, not used as the final visual verification. The subsequent native run uses a workspace-only Noto Sans CJK KR font and is stored in the parent directory.
