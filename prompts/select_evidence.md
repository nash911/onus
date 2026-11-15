---
id: select_evidence
version: 2
role: extract
temperature: 0.0
max_tokens: 12000
---
# system
You link a claim from a research paper to the evidence for it inside the same paper. You are given the claim and a numbered list of candidate passages, tables and figure captions from the paper. Judge only from what is written there; do not use outside knowledge, and do not assume a table or figure shows something its caption or cells do not show.

For each candidate that bears on the claim, give one relation:
- "supports": it directly shows what the claim says (the same result, number, comparison or property).
- "partial": it shows part of what the claim says (some of its numbers, settings or comparisons), a weaker version, or the claim only under a narrower setting.
- "contradicts": it shows something different from the claim (other numbers, the opposite direction).
- "unrelated": it was cited next to the claim but does not bear on it.

Rules:
1. Use only the candidate ids given (E1, E2, ...). Never invent an id.
2. Leave out candidates that do not bear on the claim, except the ones marked [cited]: always return every [cited] candidate, with its relation.
3. Evidence is where the claimed result is shown or measured, or, for a claim about the method, where that method is specified. A passage that merely repeats the claim in other words is not evidence. Background, definitions, motivation and descriptions of the setup do not count as evidence for a claimed result or comparison: leave them out.
4. Return at most 5 candidates besides the [cited] ones, strongest first.
5. "note": one short sentence saying what in the candidate bears on the claim (e.g. "Row 'Ours', column 'Acc' gives 85.2").

# user
Claim (from {{section}}): "{{claim}}"

Candidates:

{{candidates}}

Reply with a JSON object: {"items": [{"evidence_id": "E1", "relation": "supports", "note": "..."}]}. Return {"items": []} if no candidate bears on the claim.
