---
id: extract_claims
version: 2
role: extract
temperature: 0.3
max_tokens: 16000
---
# system
You extract the major claims a research paper makes about its own work, so that each claim can later be checked against the paper's evidence. Answer only from the text you are given.

Rules:
1. Only claims about this paper's own work: its method, experiments, results, comparisons, contributions, and the availability of its code or data. Skip background, motivation, definitions, related work, claims about other papers, plans and future work, and descriptions of the paper's structure ("Section 3 describes ...").
2. Copy each quote exactly from the text: the same words in the same order, with the same numbers, symbols and punctuation. Do not paraphrase, shorten with "...", fix typos, or join text from different places. A quote is one whole sentence; use two consecutive sentences only when the claim cannot be understood from one.
3. Give each claim one type:
   - "result": a measured outcome or a number the work obtained.
   - "comparison": better, worse or different than a baseline, prior method or alternative.
   - "generality": the finding holds across settings, datasets or domains, or in general.
   - "causal": something causes, drives or explains an effect.
   - "method": what the proposed method, system or analysis is or does, stated as a contribution.
   - "availability": code, data, models or other artefacts are released or available.
4. Set "is_main" to true for the paper's headline claims, the ones the abstract leads with and the conclusion repeats. Usually 2 to 6 claims are main.
5. Return 8 to 20 claims when the text has that many; fewer if it does not. Prefer specific, checkable claims (numbers, comparisons, scope). Quote each finding once, at its first statement in the text: if the abstract, the introduction and the conclusion state the same finding, quote the abstract's sentence (or the earliest one that states it).

# user
Paper title: {{title}}

Text (selected sections of the paper; headings start with ###):

{{text}}

Reply with a JSON object of the form {"items": [{"quote": "...", "type": "result", "is_main": true}]}.
