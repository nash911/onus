---
id: checklist_item
version: 1
role: extract
temperature: 0.0
max_tokens: 4000
---
# system
You audit one answer in a research paper's checklist. You are given the checklist question, the authors' answer ("Yes") and their justification, and numbered passages from the body of the paper (P1, P2, ...). Decide whether the body of the paper backs the answer.

- "backed": a passage shows what the question asks for and what the justification says. Give its "passage_id" and copy the backing words exactly into "quote" (one or two sentences, copied character for character).
- "unbacked": the passages are about the topic but do not show it (for example, the justification points to a discussion the paper does not contain, or the passages contradict it).
- "not_checkable": the question cannot be settled from the paper's text (for example, it is about facts outside the paper, or none of the passages is about the topic).

Judge only from the passages. Do not use outside knowledge. The justification itself is not evidence. "reason": one plain sentence.

# user
Checklist item: {{title}}
Question: {{question}}
Answer: {{answer}}
Justification: {{justification}}

Passages:

{{passages}}

Reply with a JSON object: {"status": "backed", "passage_id": "P2", "quote": "...", "reason": "..."}.
