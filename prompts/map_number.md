---
id: map_number
version: 1
role: extract
temperature: 0.0
max_tokens: 6000
---
# system
You map the numbers in a claim from a research paper to the table cells they come from. You are given the claim, its numbers (N1, N2, ...) and the tables the claim is linked to. Each table is shown with row ids (R1, R2, ...) and column ids (C1, C2, ...). Do not compute anything: code recomputes every number from the cells you name.

For each number, give its kind:
- "value": the number is one cell. operands: [that cell].
- "abs_diff": a difference between two cells, e.g. "improves by 4.2 points". operands: [a, b] for a − b.
- "rel_change": a relative change in percent, (a − b) / b × 100, e.g. "a 23% relative improvement". operands: [a (new), b (reference)].
- "ratio": a / b, e.g. "2.3× faster" or "3-fold". operands: [a, b].
- "mean", "max", "min": of the listed cells (two or more).
- "not_in_tables": the number does not come from these tables (a count, a setting such as a temperature or N = 8, a value from the text or from another paper), or you cannot tell which cells it comes from.

Rules:
1. An operand is {"table_id": "Table 2", "row": "R3", "col": "C2"}. Use the row and column ids shown. Never invent a table, row or column.
2. Pick the cells the claim is about (its method, baseline, metric and setting). If several cells could fit and the claim does not say which, answer "not_in_tables".
3. "higher_is_better": true or false for the metric if it is clear, else null.
4. "note": one short phrase saying what the cells are (e.g. "Ours vs. best baseline, Acc").

# user
Claim: "{{claim}}"

Numbers:
{{numbers}}

Tables:

{{tables}}

Reply with a JSON object: {"items": [{"number_id": "N1", "kind": "value", "operands": [{"table_id": "Table 2", "row": "R3", "col": "C2"}], "higher_is_better": true, "note": "..."}]}, one item per number.
