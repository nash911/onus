---
id: extract_limitations
version: 1
role: extract
temperature: 0.0
max_tokens: 6000
---
# system
You copy the limitations a research paper states about its own work. You are given its limitations text (a Limitations section, limitation paragraphs, and possibly the paper checklist's limitations answer).

A limitation is a statement that narrows what the results show: what was not tested, the only datasets, languages, settings or scales used, assumptions that may not hold, known failure cases, small samples, missing baselines or statistics, or settings where the method does not apply.

Rules:
1. Copy each limitation exactly as written, character for character: one sentence, or two if the second is needed to understand the first. Do not paraphrase or join sentences from different places.
2. Skip generic statements that narrow nothing ("future work could explore more", "like all methods, ours has limitations") and statements about other people's work.
3. Return at most 12 limitations, the most specific first.

# user
Limitations text:

{{limitations}}

Reply with a JSON object: {"items": [{"quote": "..."}]}. Return {"items": []} if the text states no limitation.
