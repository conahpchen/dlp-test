'use strict';

const menu = document.querySelector('.menu-toggle');
if (menu) {
  const nav = document.querySelector('#side-nav');
  nav.dataset.collapsed = '';
  menu.addEventListener('click', () => {
    const collapsed = nav.toggleAttribute('data-collapsed');
    menu.setAttribute('aria-expanded', String(!collapsed));
  });
}

for (const tools of document.querySelectorAll('[data-table]')) {
  const target = document.getElementById(tools.dataset.table);
  const body = target?.querySelector('tbody');
  if (!body) continue;
  const search = tools.querySelector('input');
  const previous = tools.querySelector('[data-page-prev]');
  const next = tools.querySelector('[data-page-next]');
  const status = tools.querySelector('[data-page-status]');
  const size = 12;
  let index = 0;

  function render() {
    const rows = Array.from(body.rows).filter(row => !row.hasAttribute('data-placeholder'));
    const query = search.value.trim().toLocaleLowerCase();
    const filtered = rows.filter(row => row.textContent.toLocaleLowerCase().includes(query));
    const pages = Math.max(1, Math.ceil(filtered.length / size));
    index = Math.min(index, pages - 1);
    const visible = new Set(filtered.slice(index * size, (index + 1) * size));
    for (const row of rows) row.hidden = !visible.has(row);
    previous.disabled = index === 0;
    next.disabled = index >= pages - 1;
    status.textContent = filtered.length ? `${filtered.length} 条 · 第 ${index + 1} / ${pages} 页` : '未找到匹配记录';
  }

  search.addEventListener('input', () => { index = 0; render(); });
  previous.addEventListener('click', () => { index -= 1; render(); });
  next.addEventListener('click', () => { index += 1; render(); });
  target.addEventListener('reveal-row', event => {
    const row = document.getElementById(event.detail);
    if (!row || !body.contains(row)) return;
    search.value = '';
    index = Math.floor(Array.from(body.rows).indexOf(row) / size);
    render();
    row.focus({preventScroll:true});
  });
  new MutationObserver(() => { index = 0; render(); }).observe(body, {childList:true});
  render();
}
