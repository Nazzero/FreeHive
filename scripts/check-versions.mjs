#!/usr/bin/env node
import { readFileSync } from 'fs';

const pkg = JSON.parse(readFileSync('package.json', 'utf-8')).version;
const tauri = JSON.parse(readFileSync('src-tauri/tauri.conf.json', 'utf-8')).version;
const cargo = readFileSync('src-tauri/Cargo.toml', 'utf-8')
    .match(/^version\s*=\s*"(.+?)"/m)?.[1];

console.log(`package.json:     ${pkg}`);
console.log(`tauri.conf.json:  ${tauri}`);
console.log(`Cargo.toml:       ${cargo}`);

if (pkg !== tauri || pkg !== cargo) {
    console.error('\n❌ Version mismatch — all three must be identical before building.');
    process.exit(1);
}
console.log('\n✅ All versions match.');
