import pathlib
pathlib.Path("tests/_v.json").write_text('{"prompt":"What is a mutex? Answer in exactly two sentences.","policy":"sla-aware"}', encoding="utf-8")
