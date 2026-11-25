const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const format = new Intl.NumberFormat("zh-CN");
const state = { groups: [], scope: "", query: "", page: 1, request: 0, resetting: false, target: null };
const migration = { pendingPage: 1, pendingRequest: 0, pendingQuery: "", selected: null, operation: null, saving: false };
const labels = ["玩家", "收藏", "最爱", "愿望单", "热度", "管理"];
const settings = {
  draw_hourly_limit: ["每小时抽卡", " 次"],
  harem_max_size: ["收藏上限", " 名"],
  claim_cooldown: ["结婚冷却", " 秒"],
  draw_cooldown: ["抽卡冷却", " 秒"],
  draw_time_range: ["开放时段", ""],
};

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text != null) result.textContent = String(text);
  if (className) result.className = className;
  return result;
}

function feedback(message = "", error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}

function switchView(view) {
  for (const name of ["game", "migration"]) {
    $(name + "-view").hidden = name !== view;
    $(name + "-tab").setAttribute("aria-pressed", name === view);
  }
  feedback();
}

function character(entry) {
  const result = node("div");
  result.append(node("strong", entry.name), node("span", [entry.source, `#${entry.id}`].filter(Boolean).join(" · "), "detail"));
  return result;
}

function collection(items) {
  if (!items.length) return node("span", "—", "muted");
  const result = node("details");
  const list = node("ul", null, "character-list");
  for (const entry of items) {
    const li = node("li");
    li.append(character(entry));
    list.append(li);
  }
  result.append(node("summary", `${items.length} 名`), list);
  return result;
}

function playerRow(player) {
  const playerCell = node("div");
  playerCell.append(node("strong", player.name || "玩家"), node("span", player.pending ? "旧服存档待关联" : player.uid, "detail"));
  const reset = node("button", "重置冷却");
  reset.type = "button";
  reset.disabled = !player.can_reset;
  reset.addEventListener("click", () => {
    if (state.resetting) return;
    state.target = { scope: state.scope, uid: player.uid, label: player.name || player.uid };
    $("reset-message").textContent = `将清除「${state.target.label}」在此群的本小时抽卡次数和结婚冷却。`;
    const dialog = $("reset-dialog");
    dialog.returnValue = "";
    dialog.showModal();
  });
  const contents = [playerCell, collection(player.partners), player.favorite ? character(player.favorite) : node("span", "—", "muted"),
    collection(player.wishes), node("span", player.heat == null ? "—" : format.format(player.heat)), reset];
  const tr = node("tr");
  for (const [index, content] of contents.entries()) {
    const td = node("td");
    td.dataset.label = labels[index];
    td.append(content);
    tr.append(td);
  }
  return tr;
}

function renderListing(data) {
  const values = Object.entries(settings).map(([key, [name, unit]]) => {
    const part = node("div");
    part.append(node("dt", name), node("dd", `${data.settings[key]}${unit}`));
    return part;
  });
  $("settings").replaceChildren(...values);
  $("settings").hidden = false;
  $("rows").replaceChildren(...data.items.map(playerRow));
  $("players").hidden = data.items.length === 0;
  $("empty").hidden = data.items.length > 0;
  $("empty").textContent = state.query ? "没有符合搜索条件的玩家。" : "此群暂无玩家记录。";
  const pages = Math.max(1, Math.ceil(data.total / data.size));
  $("pager").hidden = !data.total;
  $("page-label").textContent = `共 ${format.format(data.total)} 条 · 第 ${data.page} / ${pages} 页`;
  $("previous").disabled = data.page <= 1;
  $("next").disabled = data.page >= pages;
}

async function loadPlayers() {
  const request = ++state.request;
  $("game-view").setAttribute("aria-busy", Boolean(state.scope));
  for (const id of ["players", "pager", "settings"]) $(id).hidden = true;
  $("empty").hidden = false;
  $("empty").textContent = state.scope ? "正在读取玩家存档…" : "暂无群聊存档。群内开始使用游戏指令后，记录会显示在这里。";
  $("rows").replaceChildren();
  if (!state.scope) return;
  try {
    const data = await bridge.apiGet("console/players", { scope: state.scope, q: state.query, page: state.page });
    if (request !== state.request) return;
    const lastPage = Math.max(1, Math.ceil(data.total / data.size));
    if (data.page > lastPage) {
      state.page = lastPage;
      await loadPlayers();
      return;
    }
    renderListing(data);
  } catch (error) {
    if (request !== state.request) return;
    $("empty").textContent = "读取玩家存档失败。";
    feedback(error?.message || "请刷新重试。", true);
  } finally {
    if (request === state.request) $("game-view").setAttribute("aria-busy", "false");
  }
}

async function loadOverview() {
  const data = await bridge.apiGet("console/overview");
  state.groups = data.groups;
  $("group-count").textContent = format.format(data.groups.length);
  $("player-count").textContent = format.format(data.players);
  const choices = data.groups.map((entry) => {
    const option = node("option", `${entry.group} · ${entry.platform} · ${format.format(entry.players)} 名玩家`);
    option.value = entry.scope;
    return option;
  });
  if (!choices.length) choices.push(node("option", "暂无群聊"));
  $("group").replaceChildren(...choices);
  if (!data.groups.some((entry) => entry.scope === state.scope)) {
    state.scope = data.groups.at(0)?.scope || "";
    state.query = $("query").value = "";
    state.page = 1;
  }
  $("group").value = state.scope;
  for (const id of ["group", "query", "search"]) $(id).disabled = !state.scope;
  await loadPlayers();
}

async function resetFromDialog() {
  const target = state.target;
  state.target = null;
  if ($("reset-dialog").returnValue !== "confirm" || !target || state.resetting) return;
  state.resetting = true;
  feedback("正在重置玩家冷却…");
  try {
    await bridge.apiPost("console/reset", { scope: target.scope, uid: target.uid });
    feedback(`已重置「${target.label}」的抽卡次数和结婚冷却。`);
    await loadPlayers();
  } catch (error) {
    feedback(error?.message || "重置失败，请重试。", true);
  } finally {
    state.resetting = false;
  }
}

async function reload() {
  $("refresh").disabled = true;
  feedback();
  try {
    await loadOverview();
    await loadMigration();
  } catch (error) {
    $("game-view").setAttribute("aria-busy", "false");
    $("empty").textContent = "读取群聊存档失败。";
    feedback(error?.message || "无法读取存档，请刷新重试。", true);
  } finally {
    $("refresh").disabled = false;
  }
}

async function loadMigration() {
  try {
    const data = await bridge.apiGet("console/migration");
    const groups = data.source_groups;
    $("linked-count").textContent = format.format(data.linked);
    if (!groups.length) {
      $("migration-status").textContent = "未导入旧服存档。导入迁移包后，可在这里关联新旧群聊并查看待恢复的账号。";
      $("migration-info").hidden = true;
      return;
    }
    const mapped = groups.filter((group) => group.target_scope);
    const mappedScopes = new Set(mapped.map((group) => group.target_scope));
    const profiles = groups.reduce((sum, group) => sum + group.profiles, 0);
    const accounts = groups.reduce((sum, group) => sum + group.accounts, 0);
    $("migration-status").textContent = `已导入 ${groups.length} 个旧群的 ${accounts} 个账号，其中 ${profiles} 个已采集昵称或群名片。已关联 ${mapped.length} 个群。`;
    $("migration-info").hidden = false;
    const source = groups.filter((group) => !group.target_scope).map((group) => [group.source_group, `${group.source_group} · ${group.accounts} 个账号`]);
    const targets = state.groups.filter((group) => !mappedScopes.has(group.scope)).map(groupOption);
    fillSelect("old-group", source, "暂无未关联旧群");
    fillSelect("target-group", targets, "暂无可用目标群");
    $("map-submit").disabled = !source.length || !targets.length;
    const pendingGroups = mapped.map((group) => {
      const newGroup = state.groups.find((item) => item.scope === group.target_scope);
      const label = newGroup ? groupOption(newGroup)[1] : group.target_scope;
      return [group.target_scope, `${group.source_group} → ${label} · 已关联 ${group.linked}`];
    });
    $("pending-panel").hidden = !pendingGroups.length;
    fillSelect("mapped-group", pendingGroups, "尚未关联群聊");
    await loadPending();
  } catch (error) {
    $("linked-count").textContent = "—";
    $("migration-status").textContent = error?.message || "迁移状态暂不可用。";
    $("migration-info").hidden = true;
  }
}

function groupOption(group) {
  return [group.scope, `${group.group} · ${group.platform}`];
}

function fillSelect(id, entries, emptyLabel) {
  const select = $(id);
  const current = select.value;
  const choices = entries.length ? entries : [["", emptyLabel]];
  select.replaceChildren(...choices.map(([value, label]) => {
    const option = node("option", label);
    option.value = value;
    return option;
  }));
  if (choices.some(([value]) => value === current)) select.value = current;
  select.disabled = !entries.length;
}

function chooseOldPlayer(row, label) {
  migration.selected = { scope: $("mapped-group").value, oldUid: row.old_uid };
  $("link-selected").textContent = `旧账号 ${label}`;
  $("link-player").hidden = false;
  $("new-player").value = "";
  $("new-player").focus();
}

function pendingRow(row) {
  const li = node("li");
  const label = [...new Set([row.card, row.nickname, row.old_uid].filter(Boolean))].join(" · ");
  const choose = node("button", "选择");
  choose.type = "button";
  choose.setAttribute("aria-label", `选择旧账号 ${label}`);
  choose.addEventListener("click", () => chooseOldPlayer(row, label));
  li.append(node("span", label), choose);
  return li;
}

async function loadPending() {
  const current = ++migration.pendingRequest;
  const scope = $("mapped-group").value;
  migration.selected = null;
  $("link-player").hidden = true;
  $("pending-rows").replaceChildren();
  $("pending-pager").hidden = true;
  if (!scope) return;
  $("pending-status").textContent = "正在读取待关联账号…";
  try {
    const data = await bridge.apiGet("console/pending", { scope, q: migration.pendingQuery, page: migration.pendingPage });
    if (current !== migration.pendingRequest) return;
    const last = Math.max(1, Math.ceil(data.total / data.size));
    if (data.page > last) {
      migration.pendingPage = last;
      await loadPending();
      return;
    }
    $("pending-rows").replaceChildren(...data.items.map(pendingRow));
    $("pending-status").textContent = data.total ? "" : "没有符合条件的待关联账号。";
    $("pending-pager").hidden = !data.total;
    $("pending-page-label").textContent = `共 ${format.format(data.total)} 条 · 第 ${data.page} / ${last} 页`;
    $("pending-previous").disabled = data.page <= 1;
    $("pending-next").disabled = data.page >= last;
  } catch (error) {
    if (current === migration.pendingRequest) $("pending-status").textContent = error?.message || "读取待关联账号失败。";
  }
}

function confirmMigration(route, body, message) {
  if (migration.saving) return;
  migration.operation = { route, body };
  $("confirm-message").textContent = message;
  const dialog = $("migration-dialog");
  dialog.returnValue = "";
  dialog.showModal();
}

async function saveMigration() {
  const operation = migration.operation;
  migration.operation = null;
  if ($("migration-dialog").returnValue !== "confirm" || !operation || migration.saving) return;
  migration.saving = true;
  feedback("正在保存关联…");
  try {
    const data = await bridge.apiPost(`console/${operation.route}`, operation.body);
    await loadOverview();
    await loadMigration();
    const conflicts = data.conflicts ?? 0;
    feedback(data.message || (conflicts ? `已关联群聊。${conflicts} 个角色在新群已有归属，保留了新群的记录。` : "已保存群聊关联。"));
  } catch (error) {
    feedback(error?.message || "保存关联失败，请重试。", true);
  } finally {
    migration.saving = false;
  }
}

async function start() {
  if (!bridge) throw new Error("请从 AstrBot 管理面板打开此页。");
  await bridge.ready();
  $("refresh").addEventListener("click", reload);
  $("game-tab").addEventListener("click", () => switchView("game"));
  $("migration-tab").addEventListener("click", () => switchView("migration"));
  $("group").addEventListener("change", () => {
    state.scope = $("group").value;
    state.page = 1;
    feedback();
    void loadPlayers();
  });
  $("filters").addEventListener("submit", (event) => {
    event.preventDefault();
    state.query = $("query").value.trim();
    state.page = 1;
    feedback();
    void loadPlayers();
  });
  for (const [id, offset] of [["previous", -1], ["next", 1]]) {
    $(id).addEventListener("click", () => {
      state.page += offset;
      void loadPlayers();
    });
  }
  $("reset-dialog").addEventListener("close", resetFromDialog);
  $("mapping").addEventListener("submit", (event) => {
    event.preventDefault();
    const source = $("old-group").value;
    const target = $("target-group");
    if (source && target.value) {
      confirmMigration("map", { scope: target.value, source_group: source }, `将旧群 ${source} 的存档关联到 ${target.selectedOptions[0].textContent}。请核对群聊。`);
    }
  });
  $("pending-filter").addEventListener("submit", (event) => {
    event.preventDefault();
    migration.pendingPage = 1;
    migration.pendingQuery = $("pending-query").value.trim();
    void loadPending();
  });
  $("mapped-group").addEventListener("change", () => {
    migration.pendingPage = 1;
    void loadPending();
  });
  for (const [id, offset] of [["pending-previous", -1], ["pending-next", 1]]) {
    $(id).addEventListener("click", () => {
      migration.pendingPage += offset;
      void loadPending();
    });
  }
  $("link-player").addEventListener("submit", (event) => {
    event.preventDefault();
    const uid = $("new-player").value.trim();
    if (!uid || !migration.selected) return;
    const { scope, oldUid } = migration.selected;
    confirmMigration("link", { scope, uid, old_uid: oldUid }, `将旧账号 ${oldUid} 的存档恢复到目标群玩家 ${uid}。请先确认两者属于同一人。`);
  });
  $("migration-dialog").addEventListener("close", saveMigration);
  await reload();
}

start().catch((error) => {
  $("game-view").setAttribute("aria-busy", "false");
  $("empty").textContent = "无法连接 AstrBot 管理面板。";
  feedback(error?.message || "无法打开数据管理页。", true);
});
