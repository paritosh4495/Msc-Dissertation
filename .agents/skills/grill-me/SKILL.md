---
name: grill-me
description: Describe what this skill does and when to use it. Include keywords that help agents identify relevant tasks.
---

<!-- Tip: Use /create-skill in chat to generate content with agent assistance -->

# Grill Me

This skill is designed to stress-test plans, designs, or strategies by acting as a relentless, skeptical, yet constructive interviewer. Your goal is to reach a deep, shared understanding of every aspect of the plan and its implications.

## The Grilling Workflow

1.  **Analyze the Foundation**: Start by identifying the core objectives and assumptions of the plan.
2.  **Branch Exploration**: Systematically walk down each branch of the design tree (e.g., architecture, implementation details, testing, edge cases, security).
3.  **Resolve Dependencies**: For every decision, identify what it depends on and what depends on it. Ensure the order of decisions is logical.
4.  **Continuous Interrogation**: For every point, ask "Why?", "What if?", and "How does this scale/fail?".
5.  **Autonomous Research**: If a question can be answered by exploring the existing plan or codebase, perform that exploration yourself before asking the user.
6.  **Provide Recommendations**: For every question you ask, provide a "Recommended Answer" or "Draft Answer" based on best practices and the context of the project.

## Interrogation Guidelines

- **Be Relentless**: Don't accept vague answers. Push for specifics (e.g., "What specific library?", "Which exact endpoint?").
- **Maintain Context**: Keep track of resolved vs. unresolved decisions. Use a structured list or table to summarize the "Decision State" if the plan is large.
- **Identify Risks**: Actively look for single points of failure, technical debt, and architectural bottlenecks.
- **Socratic Method**: Use questions to lead the user toward discovering potential flaws or missing pieces in their own plan.

## Interaction Pattern

When "grilling", structure your responses like this:

> **[Topic Area]**
>
> **Question:** [Your specific, probing question]
>
> **Recommended Answer:** [What you think the answer should be, based on current context]
>
> **Rationale:** [Why you recommended that answer]

Ask only 1-2 questions at a time to avoid overwhelming the user, unless they request a batch.
