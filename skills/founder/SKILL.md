---
name: founder
description: "Foundry Strategic Dreaming Service. Provides the MCTS (Monte Carlo Tree Search) and Sandbox Simulation logic used by the 'start' skill to vet architectural design options before human approval."
---

# dynos-work: Founder (The Dreaming Engine)

The **Founder** is the strategic "Imagination" of the Foundry. It doesn't just write code; it simulates reality to prove which architectural path has the highest ROI.

## **Service 1 — Strategic Interrogation (Insight Generation)**

When called by `skills/start/SKILL.md` during discovery:
1.  **Trajectory Search**: Use the **State Signature ($)** to retrieve the top 3 successful trajectories from `trajectories.json`.
2.  **Gaps Search**: Identify the "High Entropy" (uncertain) parts of the prompt based on past failures in similar domains.
3.  **Insight Output**: Provide the `start` skill with 3-5 targeted questions that will uncover hidden complexity.

## **Service 2 — MCTS Architecture Playouts (Dreaming)**

When called to vet a **High-Risk Design Option**:
1.  **The Sandbox**: Initialize a simulation environment at `/tmp/dynos-dream-{id}/`.
2.  **Playout**: Draft the core implementation for the design option (e.g., "Architecture A: NoSQL").
3.  **Autonomous Audit**: Run an Opus-level Security and Performance audit on the drafted code within the sandbox.
4.  **Reward Attribution**: Calculate the **Discovery Reward** (How well did this architecture meet the implicit goals in simulation?).

## **Service 3 — The Design Certificate**

For every vetted option, output a **Design Certificate** to be presented in the `start` skill's design-decisions gate:
*   **Result**: PASS/FAIL.
*   **Performance Metrics**: Instruction density, cyclomatic complexity, and dependency breadth.
*   **Security Score**: Number of findings generated in the sandbox.
*   **Recommendation**: "Elite", "Standard", or "High Risk."

## **Hard Rules**

- **Supportive, Not Bypassing**: The Founder never bypasses the Spec Review. It provides the **evidence** the user needs to approve the spec.
- **Evidence-First**: Never recommend an architecture in the design phase unless a sandbox playout has been completed.
- **RL-Guided**: Every simulation must be seeded with the "winning" actions from past trajectories to ensure continuous improvement.
