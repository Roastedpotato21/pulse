# Permanent Sandbox-First Workflow Rule

Before modifying any project files, the agent must adhere to the following strict sandbox workflow using the physical `.sandbox/` directory:

1. **Sandbox Environment**: All file edits, creations, deletions, formatting, dependency installations, and tests must occur ONLY inside the dedicated `.sandbox/workspace` directory located at the project root.
2. **No Duplicates**: Never create duplicate files (e.g., `README (1).md`, `file_copy.py`, `file_new.py`). If a file already exists in the sandbox or project, always edit it directly in place. Do not create new copies.
3. **Diff Generation**: After all changes are complete, compare the `.sandbox/workspace` with the original workspace and generate a diff.
4. **Show Changes**: Display a summary to the user containing:
   - Modified files
   - Created files
   - Deleted files
   - Summary of changes
5. **Request Approval**: Ask the exact question below and wait for the user's response:
   
   ⚠️ Apply sandbox changes to the real project? (Y/N)

6. **Apply Changes**: Only if the user replies "Y" (or "yes"), apply the changes from the sandbox to the original workspace. If the user replies "N", delete the sandbox contents and leave the real project unchanged.
7. **Sandbox Cleanup**: Automatically clean the `.sandbox/temp` directory after every operation.
8. **Pre-Modification Rule**: Before making any initial changes (including within the sandbox), you must still show the files that will be modified and ask for approval according to the workspace safety rule ("⚠️ This action will modify your project. Do you want to continue? (Y/N)").
9. **Read-only Exemption**: Read-only operations (searching, reading, analyzing, explaining, indexing, status, doctor, git status, etc.) do not require confirmation or a sandbox.
