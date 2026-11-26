const 投递台 = {
  async getJSON(url, opt) {
    const r = await fetch(url, opt);
    if (r.status === 401) { location.href = '/login'; throw new Error('未登录'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.错误 || data.error || r.statusText);
    return data;
  },
  async postJSON(url, body) {
    return this.getJSON(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  },
  安全文本(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  },
  徽章(状态) {
    const span = document.createElement('span');
    span.className = '徽章 ' + (状态 || '');
    span.textContent = 状态 || '—';
    const f = this.命运(状态);
    if (f && f.文) span.title = f.文;
    return span;
  },
  命运徽章(状态) {
    const wrap = document.createElement('span');
    wrap.style.display = 'inline-flex';
    wrap.style.flexDirection = 'column';
    wrap.style.gap = '2px';
    wrap.appendChild(this.徽章(状态));
    const f = this.命运(状态);
    if (f && f.文) {
      const small = document.createElement('small');
      small.className = '命运句';
      small.textContent = f.文;
      wrap.appendChild(small);
    }
    return wrap;
  },
  命运表: {
    '待投': { 调: 'warn', 文: '命运：排队待签名投递，尚未打端点' },
    '投递中': { 调: 'info', 文: '命运：已占端点通道，等待对端结果/可重试' },
    '已投递': { 调: 'ok', 文: '命运：对端 200 签收，结案成功' },
    '成功': { 调: 'ok', 文: '尝试成败：成功（200 签收）' },
    '失败': { 调: 'danger', 文: '尝试成败：失败（500/强制失败），计连续失败与重试' },
    '死信': { 调: 'danger', 文: '命运：重试耗尽入死信匣，可重投或作废' },
    '待处置': { 调: 'danger', 文: '命运：死信待处置（重投/作废）' },
    '已重投': { 调: 'info', 文: '命运：死信已重投回队列' },
    '已作废': { 调: 'danger', 文: '命运：人工作废结案' },
    '熔断旁路': { 调: 'cb', 文: '中文旁路：端点熔断闸开，未打端点、未写成功壳，票留待复位后续投' },
    '熔断': { 调: 'cb', 文: '熔断：连续失败打满阈，现行投递拒或旁路' },
    '假绿': { 调: 'danger', 文: '假绿：熔断后仍有成功回刷，名册对账判撕裂' },
    '正常': { 调: 'ok', 文: '熔断闭合：端点正常' },
    '停用': { 调: 'muted', 文: '端点人工停用中' },
  },
  命运(状态) {
    return this.命运表[状态] || { 调: 'muted', 文: 状态 ? ('命运：' + 状态) : '' };
  },
  熔断灯(灯, 文案) {
    const span = document.createElement('span');
    span.className = '灯 ' + (灯 || '灰');
    const dot = document.createElement('i');
    dot.className = '点';
    const txt = document.createElement('span');
    txt.textContent = 文案 || ({ 绿: '正常绿灯', 黄: '连失黄灯', 红: '熔断红灯', 灰: '停用灰灯', 假绿: '假绿黑灯' }[灯] || '状态灯');
    span.appendChild(dot); span.appendChild(txt);
    span.title = '熔断灯：' + txt.textContent;
    return span;
  },
  行(html) {
    const tr = document.createElement('tr');
    tr.innerHTML = html;
    return tr;
  },
  空态(msg) { return msg || '暂无数据'; },
};
