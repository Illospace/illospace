import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const threadApiSource = readFileSync(
  new URL('../features/threads/api/threadApi.ts', import.meta.url),
  'utf8',
);

function threadApiPickerNames(source) {
  const sourceFile = ts.createSourceFile('threadApi.ts', source, ts.ScriptTarget.Latest, true);
  const names = new Set();

  function visit(node) {
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      node.expression.text === 'pickTypedApiMethods'
    ) {
      const [firstArg] = node.arguments;
      if (firstArg && ts.isArrayLiteralExpression(firstArg)) {
        for (const element of firstArg.elements) {
          if (ts.isStringLiteralLike(element)) names.add(element.text);
        }
      }
    }

    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return names;
}

function threadApiDestructuredExports(source) {
  const sourceFile = ts.createSourceFile('threadApi.ts', source, ts.ScriptTarget.Latest, true);
  const names = [];

  function visit(node) {
    if (
      ts.isVariableDeclaration(node) &&
      node.initializer &&
      ts.isIdentifier(node.initializer) &&
      node.initializer.text === 'threadApi' &&
      ts.isObjectBindingPattern(node.name)
    ) {
      for (const element of node.name.elements) {
        if (ts.isIdentifier(element.name)) names.push(element.name.text);
      }
    }

    ts.forEachChild(node, visit);
  }

  visit(sourceFile);
  return names;
}

test('thread API picker includes every destructured runtime export', () => {
  const pickedNames = threadApiPickerNames(threadApiSource);
  const exportedNames = threadApiDestructuredExports(threadApiSource);

  assert.deepEqual(
    exportedNames.filter((name) => !pickedNames.has(name)),
    [],
  );
});
