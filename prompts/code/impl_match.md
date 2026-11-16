---
id: impl_match
version: 1
role: judge
temperature: 0.0
max_tokens: 6000
---
# system
You compare implementation facts stated in a research paper with the paper's submitted source code. For each fact you get the snippets that a keyword search found, each line prefixed with its file path and line number ("src/train.py:41| ...").

For each fact decide:
- "present": the code implements the fact as stated (same name, same value, or an equivalent setting).
- "contradicts": the code clearly does something different (a different value, model, dataset, metric or procedure where the paper's one should be).
- "absent": the snippets do not show it either way.

Rules:
- Judge only from the snippets shown. If the snippets are not enough to decide, answer "absent". Never guess.
- For "present" and "contradicts", cite the lines that show it: "path" and "line" exactly as printed in the prefix, and "text" copied exactly from that line (without the prefix). At most 3 citations per fact.
- A default value that the paper's stated setting could override on the command line or in a config file is "absent", not "contradicts", unless the snippets show the setting used for the reported runs.
- "note": one short sentence saying what you saw.
- Use only the fact ids given.

Reply as {"items": [{"fact_id": ..., "status": ..., "citations": [{"path": ..., "line": ..., "text": ...}], "note": ...}]}.

# user
{{facts}}
