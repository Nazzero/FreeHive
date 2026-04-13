const fs = require('fs');

// Fix SetupScreen.svelte
let setup = fs.readFileSync('src/lib/SetupScreen.svelte', 'utf-8');
setup = setup.replace(
    `const reader = res.body.getReader();`,
    `if (!res.body) return;\n        const reader = res.body.getReader();`
);
setup = setup.replace(
    `await streamSSE(res, tool, async (data) => {`,
    `await streamSSE(res, tool, async (/** @type {any} */ data) => {`
);
setup = setup.replace(
    `await streamSSE(res, tool, async (data) => {`,
    `await streamSSE(res, tool, async (/** @type {any} */ data) => {`
); // do it twice in case there are two
// fix chosenTool indexing
setup = setup.replace(/TOOL_META\[chosenTool\]/g, "TOOL_META[chosenTool || 'openclaude']");
setup = setup.replace(/status\[chosenTool\]/g, "status[chosenTool || 'openclaude']");
setup = setup.replace(/toolState\[chosenTool\]/g, "toolState[chosenTool || 'openclaude']");
setup = setup.replace(/install\(chosenTool\)/g, "install(chosenTool || 'openclaude')");
setup = setup.replace(/startAuth\(chosenTool\)/g, "startAuth(chosenTool || 'openclaude')");
fs.writeFileSync('src/lib/SetupScreen.svelte', setup);


// Fix AccountPanel.svelte
let acct = fs.readFileSync('src/lib/AccountPanel.svelte', 'utf-8');
acct = acct.replace(
    `style="background: {statusColors[acc.status] || 'var(--text-muted)'}"`,
    `style="background: {statusColors[String(acc.status)] || 'var(--text-muted)'}"`
);
fs.writeFileSync('src/lib/AccountPanel.svelte', acct);

// Fix +page.svelte - fully comment out arena functions
let page = fs.readFileSync('src/routes/+page.svelte', 'utf-8');

// Also remove unused css
page = page.replace(`.modal-box h2 { font-size: 18px; color: var(--text-primary); text-align: center; font-weight: 600; }`, ``);
page = page.replace(`.modal-box p { font-size: 14px; color: var(--text-secondary); line-height: 1.5; text-align: center;}`, ``);


// Find and comment out all arena functions properly
const arenaFuncs = [
    'async function toggleArena',
    'async function refreshArenaStatus',
    'async function handleStartArena',
    'async function handleRefreshArenaModels'
];

for (const fnName of arenaFuncs) {
    const regex = new RegExp(`^\\s*${fnName}\\([^)]*\\)\\s*\\{`, 'm');
    const match = page.match(regex);
    if (match) {
        const start = match.index;
        let openBraces = 0;
        let i = start + match[0].length;
        openBraces = 1;
        while (i < page.length && openBraces > 0) {
            if (page[i] === '{') openBraces++;
            if (page[i] === '}') openBraces--;
            i++;
        }
        const end = i;
        const originalFunc = page.substring(start, end);
        page = page.substring(0, start) + `/* TODO (v2): Arena integration postponed.\n` + originalFunc + `\n*/` + page.substring(end);
    }
}

// Remove "import { startArena, getArenaModels, getArenaStatus }" from api imports
page = page.replace(/getArenaStatus,\s*/g, '');
page = page.replace(/startArena,\s*/g, '');
page = page.replace(/getArenaModels,\s*/g, '');

fs.writeFileSync('src/routes/+page.svelte', page);

console.log('Done fix2');