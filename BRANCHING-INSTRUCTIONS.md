# Bringing this chat's work onto a new branch

The zip `louise-adversarial.zip` contains the full repository in its final
state after this session: the seven rewritten documents, the code increment,
the migration plan, plus the two changes made in this chat — the new
`docs/08-adversarial.md` and the corrected `docs/notes/lineage.md`.

Your local folder is already a git repo connected to GitHub. You do NOT
re-clone. You create a branch, drop these files over your working tree,
review the diff, and commit.

## Steps

1. Open a terminal in your existing repo folder (the one connected to
   `github.com/sourstar/louise-rag`). Make sure your current work is clean
   or committed first:

       git status

2. Start from an up-to-date main and create the new branch:

       git checkout main
       git pull
       git checkout -b louise-adversarial

3. Unzip `louise-adversarial.zip` into the SAME folder, letting it overwrite
   existing files. On Windows PowerShell, from inside the repo folder:

       Expand-Archive -Path ..\louise-adversarial.zip -DestinationPath . -Force

   (adjust the path to wherever you saved the zip). The zip's top level is
   the repo contents, so files land in the right place.

4. See exactly what changed against your previous state:

       git status
       git diff

   The meaningful changes you expect to see are:
     - docs/08-adversarial.md  (new file)
     - docs/notes/lineage.md   (adoption row + section 7.3 rewritten)
   plus whatever else differs from your current tab state.

5. Stage and commit:

       git add -A
       git commit -m "Adversarial chapter: no-reputation defences, declared work parameter; align lineage adoption to reproduction not trust weighting"

6. Push the new branch and set upstream:

       git push -u origin louise-adversarial

## Notes

- The zip excludes `tests/fixtures/` (heavy) and any `.git`, `__pycache__`,
  `.pyc`. If your repo already has fixtures committed, they are untouched
  because the zip simply doesn't overwrite them.
- Nothing here re-downloads or re-embeds anything.
- This branch is where the architectural rewrite will land: the declared
  work parameter (§8.5) removes the universal hardware reference, which will
  ripple into checking (§3) and sharing (§4) when you fold it back.
- Two points are settled in this chat and should guide the code rewrite:
  adoption counts reproduction, never trust-weighted announcements; and the
  work parameter is declared per collection in the Codex, not imposed by a
  reference machine.
