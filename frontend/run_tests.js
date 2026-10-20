import { readdirSync } from 'node:fs';
import { join } from 'node:path';
import { run } from 'node:test';
import { spec } from 'node:test/reporters';

function findTestFiles(dir, fileList = []) {
  const files = readdirSync(dir, { withFileTypes: true });
  for (const file of files) {
    const fullPath = join(dir, file.name);
    if (file.isDirectory()) {
      findTestFiles(fullPath, fileList);
    } else if (file.isFile() && file.name.endsWith('.test.js')) {
      fileList.push(fullPath);
    }
  }
  return fileList;
}

const testFiles = findTestFiles(join(process.cwd(), 'src'));

if (testFiles.length === 0) {
  console.error('No test files matching *.test.js found.');
  process.exit(1);
}

run({ files: testFiles })
  .compose(new spec())
  .pipe(process.stdout);
