// The menu creator, in Node with a small fake document: `node tests/js/creator_test.js`.
//
// Run by tests/test_creator_js.py, which CI runs on every Python of its matrix. Plain Node, no
// package: the fake document below is the part of the DOM the creator uses, and it refuses the
// ways of writing markup the creator must never use.
"use strict";

const assert = require("assert");
const path = require("path");

const creator = require(path.join(__dirname, "..", "..", "src", "MQTTBridge", "creator.js"));

const HOSTILE = "<img src=x onerror=alert(1)>";
const REVISION = "3f9c2e4a1b7d5c60";
const NEW_REVISION = "a1b2c3d4e5f60718";
const TAGS = new Set(["div", "p", "span", "strong", "button", "label", "input", "select",
                      "option"]);

// ------------------------------------------------------------ the fake DOM --

class Element {
  constructor(tag) {
    assert.ok(TAGS.has(tag), "an element the creator never makes: " + tag);
    this.tagName = tag;
    this.children = [];
    this.parent = null;
    this.text = "";
    this.className = "";
    this.value = "";
    this.type = "";
    this.disabled = false;
    this.selected = false;
    this.placeholder = "";
    this.maxLength = -1;
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
  }

  set textContent(value) {
    this.children = [];
    this.text = String(value);
  }

  get textContent() {
    return this.text + this.children.map((child) => child.textContent).join("");
  }

  set innerHTML(_value) {
    throw new Error("innerHTML is never written");
  }

  set outerHTML(_value) {
    throw new Error("outerHTML is never written");
  }

  insertAdjacentHTML() {
    throw new Error("insertAdjacentHTML is never called");
  }

  setAttribute(name, value) {
    assert.ok(!name.startsWith("on"), "an event handler attribute: " + name);
    this.attributes[name] = String(value);
  }

  appendChild(child) {
    child.parent = this;
    this.children.push(child);
    return child;
  }

  removeChild(child) {
    this.children.splice(this.children.indexOf(child), 1);
    return child;
  }

  get firstChild() {
    return this.children[0] || null;
  }

  addEventListener(name, handler) {
    (this.listeners[name] = this.listeners[name] || []).push(handler);
  }

  fire(name, event) {
    for (const handler of this.listeners[name] || []) {
      handler(Object.assign({preventDefault() {}}, event || {}));
    }
  }

  all() {
    return [this].concat(...this.children.map((child) => child.all()));
  }
}

const doc = {
  createElement: (tag) => new Element(tag),
};

function mount(limits) {
  const node = new Element("div");
  node.dataset = {
    texts: JSON.stringify({
      top: "Top level", group: "Group", stale: "stale", saving: "Saving",
      result_saved: "Saved.", result_waiting: "Waiting.", result_stale: "Stale.",
      result_invalid: "Refused.", result_unknown: "Answered: %s", result_no_answer: "None.",
      limit_groups: "At most %s groups.", choose_entity: "Choose.", name_group: "Name it.",
      missing: "Missing: %s", not_listed: "Not listed: %s", reload: "Reload",
    }),
    icons: JSON.stringify(["neutral", "lamp", "sofa", "garden"]),
    limits: JSON.stringify(limits || {groups: 8, top: 24, rows: 24, text: 60}),
    catalogue: "/mqttbridge/menu/catalogue.json",
    save: "/mqttbridge/menu/save",
    csrf: "token-of-the-page",
  };
  return node;
}

function catalogue(changes) {
  return Object.assign({
    v: 1,
    revision: REVISION,
    areas: ["Kitchen", HOSTILE],
    left_out: 2,
    menu: [
      {type: "group", id: "g_0a1b2c3d4e", label: HOSTILE, icon: "sofa",
       rows: [{type: "row", id: "r_5f6a7b8c9d", entity: "light.living_room", control: "slider",
               label: null, icon: null}]},
      {type: "row", id: "r_9e8d7c6b5a", entity: "0123456789abcdef", control: "toggle",
       label: null, icon: null, missing: true},
    ],
    entities: [
      {e: "light.living_room", n: HOSTILE, a: 1, c: ["toggle", "slider", "indicator"]},
      {e: "sensor.outdoor_temperature", n: "Outdoor temperature", a: null, c: ["indicator"],
       h: true, k: "diagnostic"},
      {e: "scene.movie", n: "Movie night", a: 0, c: ["button"]},
    ],
  }, changes || {});
}

function network(answers) {
  const sent = [];
  let clock = 0;
  return {
    sent,
    fetch(url, options) {
      sent.push({url, options});
      const next = answers.shift();
      if (next instanceof Error) {
        return Promise.reject(next);
      }
      return Promise.resolve({json: () => Promise.resolve(next)});
    },
    URLSearchParams,
    now: () => clock,
    wait(milliseconds) {
      clock += milliseconds;
      return Promise.resolve();
    },
    reloadPage() {
      this.reloaded = true;
    },
  };
}

async function loaded(answers, limits) {
  const node = mount(limits);
  const net = network([catalogue()].concat(answers || []));
  const page = new creator.Creator(node, doc, net);
  await page.load();
  return {node, net, page};
}

const tests = [];
function test(name, body) {
  tests.push([name, body]);
}

// ---------------------------------------------------------------- the data --

test("a catalogue that breaks the envelope is not one", () => {
  for (const broken of [null, [], {v: 2}, catalogue({v: 2}), catalogue({revision: "X"}),
                        catalogue({menu: {}}), catalogue({entities: null})]) {
    assert.strictEqual(creator.parseCatalogue(broken), null);
  }
});

test("entity entries that are not of their type are dropped", () => {
  const parsed = creator.parseCatalogue(catalogue({entities: [
    {e: 7, n: "x", c: ["toggle"]}, {e: "a.b", n: "A", c: []}, {e: "c.d", n: 5, c: ["button", 3]},
    {e: "c.d", n: "twice", c: ["toggle"]}, {e: "e.f", n: "E", a: 9, c: ["toggle"]},
  ]}));
  assert.deepStrictEqual(parsed.entities.map((entity) => entity.e), ["c.d", "e.f"]);
  assert.deepStrictEqual(parsed.byId["c.d"].c, ["button"]);
  assert.strictEqual(parsed.byId["c.d"].n, "c.d");
  assert.strictEqual(parsed.byId["e.f"].area, "");
});

test("search matches every word in the name, the entity id or the area", () => {
  const parsed = creator.parseCatalogue(catalogue());
  const ids = (query) => creator.search(parsed.entities, query, 50).map((entity) => entity.e);
  assert.deepStrictEqual(ids("outdoor"), ["sensor.outdoor_temperature"]);
  assert.deepStrictEqual(ids("SCENE kitchen"), ["scene.movie"]);
  assert.deepStrictEqual(ids("light onerror"), ["light.living_room"]);
  assert.deepStrictEqual(ids("movie outdoor"), []);
  assert.strictEqual(creator.search(parsed.entities, "", 1).length, 1);
});

test("the payload keeps ids, sends null for new ones and for empty labels", () => {
  const menu = [
    {kind: "group", id: null, label: "  Garden  path ", icon: "", rows: [
      {kind: "row", id: "r_1", entity: "light.a", control: "toggle", label: "  ", icon: ""}]},
    {kind: "row", id: null, entity: "scene.b", control: "button", label: "Go", icon: "movie",
     missing: false},
  ];
  assert.deepStrictEqual(creator.toPayload(menu), [
    {type: "group", id: null, label: "Garden path", icon: "neutral", rows: [
      {type: "row", id: "r_1", entity: "light.a", control: "toggle", label: null, icon: null}]},
    {type: "row", id: null, entity: "scene.b", control: "button", label: "Go", icon: "movie"},
  ]);
});

test("the bounds are checked before anything is sent", () => {
  const limits = {groups: 1, top: 2, rows: 1, text: 60};
  const row = {kind: "row", entity: "a.b", control: "toggle"};
  const group = (rows) => ({kind: "group", label: "G", rows});
  assert.strictEqual(creator.checkDraft([group([])], limits), null);
  assert.strictEqual(creator.checkDraft([group([]), group([])], limits), "limit_groups");
  assert.strictEqual(creator.checkDraft([row, row, row], limits), "limit_top");
  assert.strictEqual(creator.checkDraft([group([row, row])], limits), "limit_rows");
  assert.strictEqual(creator.checkDraft([{kind: "group", label: " ", rows: []}], limits),
                     "name_group");
  assert.strictEqual(creator.checkDraft([{kind: "row", entity: "", control: ""}], limits),
                     "choose_entity");
  assert.strictEqual(creator.checkDraft([{kind: "row", entity: "", control: "toggle"}], limits),
                     "choose_entity");
});

test("an answer the page has no sentence for is said as it came", () => {
  const texts = {result_saved: "Saved.", result_unknown: "Answered: %s"};
  assert.strictEqual(creator.resultText({result: "saved"}, texts), "Saved.");
  assert.strictEqual(creator.resultText({result: "postponed"}, texts), "Answered: postponed");
  assert.strictEqual(creator.resultText({result: "constructor"}, texts), "Answered: constructor");
  assert.strictEqual(creator.resultText(null, texts), "Answered: ?");
});

// ---------------------------------------------------------------- the page --

test("every string from the catalogue reaches the page as text", async () => {
  const {node} = await loaded();
  const all = node.all();
  const texts = all.map((element) => element.text).join("\n");
  assert.ok(texts.includes(HOSTILE), "the hostile name is shown, as text");
  const values = all.filter((element) => element.tagName === "input").map((input) => input.value);
  assert.ok(values.includes(HOSTILE), "the hostile group label is a field's value");
  for (const element of all) {
    for (const value of Object.values(element.attributes)) {
      assert.ok(!value.includes("<"), "markup in an attribute: " + value);
    }
  }
  assert.ok(texts.includes("Missing: 0123456789abcdef"));
});

test("the page loads the catalogue from its own address with the cookies of the page",
     async () => {
  const {net} = await loaded();
  assert.strictEqual(net.sent[0].url, "/mqttbridge/menu/catalogue.json");
  assert.strictEqual(net.sent[0].options.credentials, "same-origin");
});

test("a catalogue that is not there says why", async () => {
  const node = mount();
  const net = network([{catalogue: null, reason: "newer"}]);
  const page = new creator.Creator(node, doc, net);
  await page.load();
  assert.strictEqual(page.catalogue, null);
  assert.strictEqual(page.message.text, "newer");
});

test("a save posts the whole menu with its base and the page's token", async () => {
  const {net, page} = await loaded([{result: "invalid", path: "menu[0].label", detail: HOSTILE}]);
  await page.save();
  const request = net.sent[1];
  assert.strictEqual(request.url, "/mqttbridge/menu/save");
  assert.strictEqual(request.options.method, "POST");
  const body = request.options.body;
  assert.strictEqual(body.get("csrf"), "token-of-the-page");
  assert.strictEqual(body.get("form"), "menu_edit");
  assert.strictEqual(body.get("base"), REVISION);
  const menu = JSON.parse(body.get("menu"));
  assert.strictEqual(menu.length, 2);
  assert.strictEqual(menu[1].missing, undefined);
  assert.strictEqual(menu[1].entity, "0123456789abcdef");
  assert.strictEqual(page.message.text, "Refused.");
  assert.deepStrictEqual(page.message.detail, {path: "menu[0].label", text: HOSTILE});
  assert.strictEqual(page.busy, false);
});

test("a stale save keeps the draft on the page and offers a reload", async () => {
  const {page} = await loaded([{result: "stale", revision: NEW_REVISION}]);
  page.addGroup();
  const before = JSON.stringify(creator.toPayload(page.menu));
  await page.save();
  assert.strictEqual(page.message.text, "Stale.");
  assert.strictEqual(page.offerReload, true);
  assert.strictEqual(JSON.stringify(creator.toPayload(page.menu)), before);
  assert.strictEqual(page.dirty, true);
  page.change();
  assert.strictEqual(page.message.text, "Stale.", "the offer stays until the reload");
});

test("after a save the page waits for the catalogue with the new revision", async () => {
  const fresh = catalogue({revision: NEW_REVISION, menu: [
    {type: "group", id: "g_new0000001", label: "Garden", icon: "garden", rows: []}]});
  const {net, page} = await loaded([{result: "saved", revision: NEW_REVISION}, catalogue(),
                                    fresh]);
  page.addGroup();
  await page.save();
  assert.strictEqual(net.sent.length, 4);
  assert.strictEqual(page.catalogue.revision, NEW_REVISION);
  assert.strictEqual(page.menu[0].id, "g_new0000001");
  assert.strictEqual(page.dirty, false);
  assert.strictEqual(page.message.text, "Saved.");
});

test("the wait for the new revision ends after five seconds", async () => {
  const answers = [{result: "saved", revision: NEW_REVISION}];
  for (let index = 0; index < 20; index += 1) {
    answers.push(catalogue());
  }
  const {net, page} = await loaded(answers);
  await page.save();
  // The first fetch, the save, and eleven catalogue fetches half a second apart.
  assert.strictEqual(net.sent.length, 13);
  assert.strictEqual(page.busy, false);
});

test("a save that cannot reach the receiver is no answer and offers a reload", async () => {
  const {page} = await loaded([new Error("network")]);
  await page.save();
  assert.strictEqual(page.message.text, "None.");
  assert.strictEqual(page.offerReload, true);
});

test("a draft over the bounds is never sent", async () => {
  const {net, page} = await loaded([], {groups: 1, top: 24, rows: 24, text: 60});
  page.menu.push({kind: "group", id: null, label: "Second", icon: "neutral", rows: [],
                  uid: "x"});
  await page.save();
  assert.strictEqual(net.sent.length, 1);
  assert.strictEqual(page.message.text, "At most 1 groups.");
});

test("adding stops at the bounds", async () => {
  const {page} = await loaded([], {groups: 1, top: 3, rows: 1, text: 60});
  page.addGroup();
  assert.strictEqual(page.groups().length, 1);
  page.addRow(null);
  page.addRow(null);
  assert.strictEqual(page.menu.length, 3);
  const group = page.groups()[0];
  page.addRow(group);
  assert.strictEqual(group.rows.length, 1);
});

test("a row moves between places, and never into a full one", async () => {
  const {page} = await loaded([], {groups: 8, top: 2, rows: 1, text: 60});
  const group = page.groups()[0];
  const row = group.rows[0];
  page.relocate(row, "");
  assert.strictEqual(page.menu.length, 2, "the top level is full");
  assert.strictEqual(group.rows.length, 1);
  const top = page.menu[1];
  page.relocate(top, group.uid);
  assert.strictEqual(group.rows.length, 1, "the group is full");
  page.remove(group.rows, row);
  page.relocate(top, group.uid);
  assert.deepStrictEqual(group.rows, [top]);
  assert.strictEqual(page.menu.length, 1);
});

test("choosing an entity takes its default control when the old one does not fit", async () => {
  const {page} = await loaded();
  const row = page.newRow();
  page.menu.push(row);
  page.choose(row, page.catalogue.byId["scene.movie"]);
  assert.strictEqual(row.entity, "scene.movie");
  assert.strictEqual(row.control, "button");
  row.control = "button";
  page.choose(row, page.catalogue.byId["light.living_room"]);
  assert.strictEqual(row.control, "toggle");
});

test("typing in the picker lists matches, and Enter picks the highlighted one", async () => {
  const {node, page} = await loaded();
  const row = page.newRow();
  page.menu.push(row);
  page.render();
  const inputs = node.all().filter((element) => element.type === "search");
  const picker = inputs[inputs.length - 1];
  picker.value = "movie";
  picker.fire("input");
  picker.fire("keydown", {key: "Enter"});
  assert.strictEqual(row.entity, "scene.movie");
});

(async () => {
  let failed = 0;
  for (const [name, body] of tests) {
    try {
      await body();
      console.log("ok - " + name);
    } catch (error) {
      failed += 1;
      console.log("not ok - " + name + "\n" + (error && error.stack));
    }
  }
  console.log(tests.length + " tests, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})();
