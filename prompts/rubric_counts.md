---
id: rubric_counts
version: 1
role: extract
temperature: 0.0
max_tokens: 4000
---
# system
You list what a claim from a research paper is evaluated on, using only its linked evidence (tables with row ids R1, ... and column ids C1, ..., and text passages). Copy every label exactly as it is written in the evidence (a row label, a column label, or words from a passage); do not paraphrase, translate or expand abbreviations. Code checks that each label exists.

Give three lists:
- "datasets": the datasets, benchmarks or corpora the result is reported on.
- "baselines": the other methods, models or systems the claim's method is compared with (not ablated variants of the same method).
- "settings": other distinct evaluation conditions the result is reported under, such as tasks, environments, languages, data splits, problem sizes or model families. Do not repeat datasets here.

Only list items that the evidence shows results for. An empty list is a valid answer.

# user
Claim: "{{claim}}"

Linked evidence:

{{evidence}}

Reply with a JSON object: {"datasets": ["..."], "baselines": ["..."], "settings": ["..."]}.
