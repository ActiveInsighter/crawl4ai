import {readFile, readdir, writeFile} from 'node:fs/promises';
import {resolve, join} from 'node:path';
import {compile} from '@mdx-js/mdx';
import remarkGfm from 'remark-gfm';
import remarkFrontmatter from 'remark-frontmatter';

const root = resolve(process.argv[2] || 'artifacts/408-mdx');
async function findMdx(directory, prefix = '') {
  const files = [];
  for (const entry of await readdir(directory, {withFileTypes: true})) {
    const relative = prefix + entry.name;
    if (entry.isDirectory()) files.push(...await findMdx(join(directory, entry.name), relative + '/'));
    else if (entry.isFile() && entry.name.endsWith('.mdx')) files.push(relative);
  }
  return files;
}
const files = (await findMdx(root)).sort();
if (!files.length) throw new Error('No MDX files found');
for (const file of files) {
  await compile({value: await readFile(join(root, file), 'utf8'), path: file}, {
    remarkPlugins: [remarkGfm, remarkFrontmatter],
  });
  console.log(`[MDX] ${file}: compiled`);
}
await writeFile(join(root, 'mdx-validation.json'), JSON.stringify({
  files, compiler: '@mdx-js/mdx', all_compiled: true,
}, null, 2) + '\n');
