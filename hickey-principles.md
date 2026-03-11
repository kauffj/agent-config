# Programming Principles: Hickey's Philosophy of Simplicity

Synthesized from Rich Hickey's body of work: *Simple Made Easy*, *Are We There Yet?*, *Hammock Driven Development*, *The Value of Values*, *Spec-ulation*, and related talks.

---

## The Central Distinction: Simple vs. Easy

**Simple** (from Latin *simplex*: "one fold") means not braided together, not complected. It is an **objective** property of the artifact.
- One role, one task, one concept
- Can be reasoned about in isolation
- Independent of who is using it

**Easy** (from French *aise*: "nearby") means familiar, close at hand, low friction. It is **relative** to a person's context and skill.

> Confusing these is the root of most software complexity. Easy choices create complex systems. Simple choices may feel hard at first but pay compound dividends.

**The question to ask at every design decision:** *"Am I adding complection to this system?"* Not: *"Is this convenient right now?"*

---

## The Core Sin: Complecting

**Complecting** (the verb form of "complex") means to interleave, entwine, or braid things together that should be separate.

Complected code cannot be understood, changed, or tested in pieces — you must hold the whole braid in your head at once. This is the mechanism by which complexity destroys velocity over time.

### Common Sources of Complection to Avoid

| Complected Thing | Simpler Alternative |
|---|---|
| Stateful objects (state + identity + behavior) | Separate values, functions, and identities |
| Inheritance hierarchies | Protocols / interfaces / type classes |
| Mutable variables | Immutable values + explicit state management |
| ORM (objects complected to DB rows) | Plain data, explicit queries |
| Imperative loops with accumulation | Pure transformations (map/filter/reduce) |
| Conditionals scattered throughout | Declarative rules, data-driven dispatch |
| Hardwired component dependencies | Pass components as arguments (dependency injection) |
| Synchronous coupling | Queues, channels, events |

---

## Values vs. State vs. Identity

This is the most important conceptual clarification for writing correct concurrent and maintainable systems.

- **Value**: An immutable fact. The number 42. The string "hello". A map `{:name "Alice"}`. Values never change — you can only derive new values from them.
- **State**: The value an identity holds at a specific point in time. State is not mutable — a new state is a new value.
- **Identity**: A logical entity that we associate with a succession of values over time. An identity *has* states; it does not *be* a state.
- **Time**: The ordered sequence of states for an identity. OOP collapses time — there is only "now." This makes reasoning about concurrent programs impossible.

### Practical implications
- **Prefer returning new values** over mutating existing ones.
- **Make mutation explicit and managed** — don't bury it inside objects. When you need shared mutable state, isolate it behind a managed reference (atom, ref, agent, or equivalent).
- **Functions should be pure** — same input always produces same output, no hidden state read or written.
- **State at the boundary, not the core** — keep stateful operations at system edges (I/O, DB, external APIs), keep the core logic pure.

---

## Data Orientation

Prefer **plain data** (maps, arrays, sets, primitives) over custom classes and types.

- Data is visible, printable, inspectable, and universal.
- Custom types create a private vocabulary; maps create a shared one.
- Data can be passed through queues, serialized, logged, and compared with zero ceremony.
- Functions that operate on generic data (maps, collections) are reusable across the whole system.

> "It is better to have 100 functions operate on one data structure than 10 functions on 10 data structures." — Alan Perlis (cited approvingly by Hickey)

This does NOT mean abandon all types. It means: don't create a class when a map with a key will do. Don't create a wrapper type when the underlying value carries the meaning.

---

## Separate What from Who from How from When

Complection often happens along these dimensions:

- **What** (the logic/policy) should be separate from **Who** (which component does it).
- **How** (implementation) should be separate from **What** (specification/interface).
- **When** (timing, ordering, scheduling) should be separate from **What** (the computation).
- **Where** (location, resource) should be separate from **What** (the value).

### Practical test:
Can you change the *when* (sync vs. async) without rewriting the *what*? Can you swap the *who* (different implementation) without changing callers? If not, you have complection.

---

## Construct vs. Artifact

We are trained to evaluate programming tools (languages, frameworks, libraries) by how easy they are to **use** (the construct). We should instead evaluate them by the quality of what they **produce** (the artifact).

- A framework that is easy to get started with but produces a complected, untestable system is a bad trade.
- An approach that feels unfamiliar at first but produces simple, composable, testable code is a good trade.
- "The measure of a design is not how it felt to write it, but how it feels to change it six months later."

When evaluating a library, framework, or pattern, ask: **What does the output look like? Is it simple?** Not: *"How quickly can I get something working?"*

---

## Thinking Before Coding (Hammock-Driven Development)

Hickey argues that most bugs are not coding errors — they are **design errors** and **misunderstood requirements**. The solution is to think more and code less, at least initially.

### Before writing code:
1. **State the problem clearly** — write it down, say it aloud. If you can't articulate it, you don't understand it.
2. **Identify what you know, what you don't know, and what constraints apply.**
3. **Find prior art** — the problem has almost certainly been solved or addressed elsewhere.
4. **Evaluate at least two approaches** — document tradeoffs explicitly.
5. **Look for the flaw in your solution** — Hickey's heuristic: "If you don't have questions, you're missing something."

### The background mind:
- Sleep on hard problems. The background mind solves problems that the conscious mind cannot.
- Walk away from the screen. Load the problem into your head, then let go of active focus.
- Don't confuse "stuck and grinding" with "productive." When stuck, stop and think, don't keep typing.

---

## Simplicity Is a Choice

Simplicity doesn't happen by accident. It requires:

1. **Active vigilance** at every design decision to ask: "Am I complecting something?"
2. **Willingness to feel unfamiliar** — simple solutions are often less familiar than complex ones.
3. **Distinguishing incidental complexity** (introduced by your choices) from **inherent complexity** (required by the problem domain). Eliminate the former ruthlessly; accept the latter honestly.
4. **Evaluating the artifact** — regularly ask "if I had to explain this system to a new person, what would be hard?" That's where the complexity lives.

> "Simplicity is a choice. It's your fault if you don't have a simple system."

---

## On Abstractions

Good abstractions separate concerns. Bad abstractions create new complection.

- **Design interfaces (what) separately from implementations (how).** Never let implementation details leak into the interface.
- **Keep interfaces small** — one clear concept, not a grab bag of related functions.
- **Use polymorphism** to keep implementations separate without multiplying interfaces.
- **Don't create abstractions for hypothetical futures** — but do create them when two concrete things genuinely share a simple concept.
- **Specs over types** — describe what data should be, not how it's structured internally.

---

## On Change and Growth

Hickey's *Spec-ulation* keynote adds a crucial dimension: how systems should evolve.

- **Accretion is safe** — adding new functions, new keys to maps, new capabilities.
- **Breaking changes are never cheap** — removing or changing existing behavior forces rewrites in all consumers.
- Prefer to **grow** a system by adding, not by modifying. Keep old paths working; introduce new paths.
- This applies to APIs, schemas, function signatures, and data shapes.

---

## Red Flags: Signs of Complection in Your Code

When you see these, stop and ask whether the complection is necessary:

- A function that does two things (naming it with "and" is a tell)
- A class that holds state, knows how to persist itself, and has business logic
- A function that needs to know about its caller's context to behave correctly
- Config/options objects that grow unboundedly
- Tests that require extensive setup to isolate a single behavior
- "If I change X, I have to change Y" — any such coupling is complection
- A module that cannot be understood without understanding another module

---

## Summary: The Questions to Ask

Before every significant design decision:

1. **Is this simple?** (Not: is it familiar? Not: is it quick?)
2. **What am I complecting here?** (What concerns am I braiding together that could be separate?)
3. **Am I choosing a construct because it's easy, or because the artifact it produces is simple?**
4. **Have I thought about this long enough?** (Or am I just reaching for the keyboard to feel productive?)
5. **What is the cost of change?** (If the requirements change, how much unravels?)
6. **Is this incidental or inherent complexity?** (Did the problem require this, or did my solution introduce it?)
