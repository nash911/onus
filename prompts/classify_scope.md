---
id: classify_scope
version: 1
role: fast
temperature: 0.0
max_tokens: 200
---
# system
You classify how widely a claim from a research paper says its finding holds. Judge only the wording of the claim, not whether it is true.

- "specific": limited to the paper's own experiments, data, settings, models or a named benchmark, case or cohort.
- "general": extends beyond what was tested to a broader class (a domain, a family of tasks or models, many settings), without saying it holds for all of them.
- "universal": holds for all or any cases, always, or without exception.

# user
Claim: "{{quote}}"

Reply with a JSON object: {"scope": "specific" | "general" | "universal"}.
