---
name: trajectory
description: "Decision Transformer Sequence Modeler. Manages the SAR (State-Action-Reward) memory and performs Trajectory Retrieval for MCTS-Search guidance."
---

# dynos-work: Trajectory (The Sequence Memory)

This is the **Contextual Intelligence** layer of your Decision-Transformer architecture.

## What you do

### Step 1 -- SAR Extraction (PUSH)
Immediately after a successful task completion and `/dynos-work:learn`:
1. **Trace Reconstruction:** Reconstruct the sequence of (State, Action, Reward) for the entire task.
2. **Update Registry:** Push the sequence into `.dynos/trajectories.json`.
3. **Reward Attribution:** Assign a scalar reward relative to the **Goal** (e.g. `Return-to-Go = 1.0 Quality + 0.8 Cost`).

### Step 2 -- Behavioral Retrieval (PULL)
During the "Founder Phase" or "Planning Phase":
1. **State Signature Search:** Call the `state-encoder` to get the current module's state signature ($).
2. **Trajectory Search:** Use Vector Similarity (or a simple Weighted Hash Search) to find the 3 most similar successful trajectories in `trajectories.json`.

### Step 3 -- Sequence-Guided Search (MCTS)
During the **MCTS Tree Search**, the system:
1. **Prioritize Branches:** Give a **Policy Prior Bonus** to design options that align with the "Winning Actions" from the retrieved trajectories.
2. **Playout Pruning:** Abandon branches that match the "Action Clusters" that led to low rewards in past trajectories.

## Hard Rules
- **Trajectory Integrity:** Never modify a past trajectory; they are immutable ground truth.
- **Anonymization:** Strip all local paths and IDs from the trajectories before they are indexed for search.

