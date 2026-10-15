```markdown
# Friday Development Patterns

> Auto-generated skill from repository analysis

## Overview
This skill introduces the core development patterns and conventions used in the "Friday" TypeScript codebase. It covers file organization, code style, import/export strategies, and testing approaches. By following these guidelines, contributors can write consistent, maintainable code and collaborate effectively on the project.

## Coding Conventions

### File Naming
- Use **camelCase** for all file names.
  - Example: `userProfile.ts`, `dataFetcher.ts`

### Import Style
- Use **relative imports** for referencing other modules.
  - Example:
    ```typescript
    import { fetchData } from './dataFetcher';
    ```

### Export Style
- Use **named exports** for all exported functions, classes, or constants.
  - Example:
    ```typescript
    // dataFetcher.ts
    export function fetchData() { ... }
    export const API_URL = '...';
    ```

### Commit Patterns
- Commit messages are **freeform** (no enforced structure).
- Commonly short, average length ~20 characters.
- Prefixes are sometimes used but not required.

## Workflows

### Adding a New Feature
**Trigger:** When implementing a new functionality.
**Command:** `/add-feature`

1. Create a new camelCase file for your feature (e.g., `newFeature.ts`).
2. Use relative imports to include dependencies.
3. Export your feature using named exports.
4. Write corresponding tests in a `*.test.ts` file.
5. Commit changes with a clear, concise message.

### Fixing a Bug
**Trigger:** When addressing a reported issue or bug.
**Command:** `/fix-bug`

1. Locate the relevant file(s) using camelCase naming.
2. Apply the fix, maintaining code style conventions.
3. Update or add tests in the related `*.test.ts` file.
4. Commit with a message describing the fix.

### Writing Tests
**Trigger:** When verifying new or existing functionality.
**Command:** `/write-test`

1. Create or update a test file named `featureName.test.ts`.
2. Use the preferred (unknown) testing framework.
3. Write tests that cover expected behaviors.
4. Run tests to ensure correctness.

## Testing Patterns

- Test files follow the pattern: `*.test.ts`
  - Example: `userProfile.test.ts`
- The specific testing framework is not detected; follow existing patterns in the codebase.
- Place tests alongside or near the modules they cover.

## Commands
| Command       | Purpose                                    |
|---------------|--------------------------------------------|
| /add-feature  | Scaffold and implement a new feature       |
| /fix-bug      | Apply and commit a bug fix                 |
| /write-test   | Create or update tests for a module        |
```