# rtcheck

**Find the call that will glitch your audio, before anyone hears it.**

Some functions must never pause. An audio callback has around 1.3 ms to fill a buffer and no permission to stop for anything — no allocation, no locks, no I/O, no sleeping. Break that rule and you don't get a crash you can debug; you get a click in someone's ear.

The rule is easy to state and almost impossible to enforce by reading. The offending call is rarely in the function you wrote. It's four levels down, in a helper someone added last month, in a library you didn't write.

`rtcheck` reads your C, follows every path out of the function you nominate, and prints the route to the problem:

```
violation: mix_frame must not allocate or free memory
  mix_frame       mixer.c:87
  → apply_reverb  mixer.c:48
  → push_tap      mixer.c:40
  → ensure_capacity mixer.c:34
  → realloc
```

It never runs your program. It has no idea how long anything takes. It finds paths.

## Install

```sh
pip install rtcheck
```

The `libclang` dependency bundles its own shared library, so there's no separate LLVM install.

## Use

```sh
rtcheck src/ --entry mix_frame
```

Exit code is `1` if there are violations, so it drops straight into CI:

```yaml
- run: rtcheck src/ --entry audio_callback --entry midi_isr
```

With an existing build:

```sh
rtcheck --compile-commands build/compile_commands.json --entry mix_frame
```

## Configuration

Drop a `rtcheck.toml` next to your source. It layers over the built-in policy rather than replacing it.

```toml
[entrypoints]
functions = ["mix_frame", "process_block"]

[allow]
# Reviewed by hand and known safe despite appearances.
functions = ["rt_pool_acquire"]

[forbidden.alloc]
# Add your project's own wrappers to an existing category.
symbols = ["my_arena_grow", "buffer_reserve"]

[forbidden.gpu]
# Or define a new one.
message = "touch the GPU"
symbols = ["glBufferData", "vkQueueSubmit"]

[options]
indirect = "warn"     # warn | address-taken | ignore
flag_recursion = true
```

## Function pointers

This is where honesty matters more than a green tick.

A call through a function pointer can't be resolved by reading the source. `rtcheck` has three modes and none of them is magic:

- **`warn`** (default) — reports the call site as a blind spot and says so. It will not claim your code is clean when it can't see.
- **`address-taken`** — assumes an indirect call may reach *any* function whose address is taken anywhere in the parsed code. Over-approximate: more findings, some of them false, no silent gaps.
- **`ignore`** — for when you've established the targets are safe by other means.

Start on `warn`. Move to `address-taken` when you want the stricter answer and can absorb the noise.

## What it cannot do

Stated plainly, because a checker that oversells itself is worse than no checker:

- **It is a linter, not a proof.** `dlopen`, computed jumps, inline assembly and callbacks from third-party binaries are outside what source analysis can see.
- **It doesn't measure time.** A pure-arithmetic loop over ten million samples will pass and still blow your deadline.
- **It only knows the symbols you tell it about.** A hand-rolled allocator not listed in the config sails straight through. Adding your project's wrappers to `rtcheck.toml` is most of the work of adopting it.
- **Anything without source is opaque.** Calls into a library you only have headers for are reported as unknowns, not cleared.
- **C only, for now.** C++ needs name mangling, virtual dispatch and template instantiation handled properly; that's a real piece of work, not a flag.

Complementary to runtime checkers, not a replacement. A runtime tool tells you what actually happened on the path you actually ran. This tells you what could happen on paths you haven't hit yet, in CI, before you ship.

## How it works

1. Parse each translation unit with libclang.
2. Build a call graph keyed by USR, so `static` functions with the same name in different files stay distinct and the same external resolves to one node across the project.
3. Breadth-first search from each entry point. BFS rather than DFS so the reported path is the shortest one — the one that's quickest to read and fix.
4. Stop at forbidden leaves, record the path, and note recursion and indirect sites along the way.

Roughly 600 lines. It's meant to be readable.

## Contributing

The most valuable contribution is a **symbol list for a real framework** — JUCE, PortAudio, ALSA, CMSIS, a vendor HAL. The analysis is only as good as its knowledge of which leaves are dangerous, and that knowledge is domain expertise, not code.

After that, in rough order of usefulness: C++ support, better indirect-call resolution (type-signature matching would cut `address-taken` false positives sharply), and an annotation syntax so a function's contract lives next to the function.

## Provenance

The design and the initial implementation are Claude's (Anthropic), written in a single session in August 2026 — the idea, the architecture, the code, this README. It's maintained here by [@giobuilds](https://github.com/giobuilds), who can merge your pull request. That distinction matters: an AI can write a tool, but a repository needs a human who's still around in six months.

## Licence

MIT.
