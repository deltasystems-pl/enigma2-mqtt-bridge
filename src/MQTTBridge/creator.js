/* The menu creator: the one script of the MQTT Bridge OpenWebif page (ADR-0022).
 *
 * It builds the Home Assistant menu of this receiver from the catalogue Home Assistant publishes
 * (`catalogue.json`, the exact bytes the receiver received) and posts the edited menu to `save`,
 * which answers when Home Assistant has validated and stored it - or refused it.
 *
 * Rules this file keeps, and the tests check:
 * - Every string from the catalogue is untrusted text: anyone who can publish on the broker can
 *   write it. It reaches the page through `textContent` and form values only - never as markup,
 *   never into an attribute by string, never into an address.
 * - No `eval`, no code built from text, no address outside this page's own origin, no library.
 * - Plain ES2017, for the browsers that open a receiver's web interface.
 *
 * Home Assistant decides. The bounds below are checked here as a convenience, so that a save is
 * not sent only to be refused; the answer to a save is Home Assistant's.
 */
(function (root) {
  "use strict";

  var CONTROLS = ["toggle", "slider", "indicator", "button", "selector", "input"];

  // ------------------------------------------------------------------- text --

  function fmt(template) {
    var values = Array.prototype.slice.call(arguments, 1);
    var index = 0;
    return String(template).replace(/%s/g, function () {
      var value = index < values.length ? values[index] : "";
      index += 1;
      return String(value);
    });
  }

  function textOf(value, limit) {
    var text = typeof value === "string" ? value : "";
    return limit ? text.slice(0, limit) : text;
  }

  // -------------------------------------------------------------- catalogue --

  // A value by a key that came from the catalogue: an own property or nothing. Every map such a
  // key indexes is made with no prototype, and is read through here as well, so an entity id
  // such as "constructor" or "__proto__" is an entity id and nothing else.
  function own(map, key) {
    return typeof key === "string" && Object.prototype.hasOwnProperty.call(map, key)
      ? map[key] : null;
  }

  function isRevision(value) {
    return typeof value === "string" && /^[0-9a-f]{16}$/.test(value);
  }

  function parseRow(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      return null;
    }
    return {
      kind: "row",
      id: typeof raw.id === "string" ? raw.id : null,
      entity: typeof raw.entity === "string" ? raw.entity : "",
      control: typeof raw.control === "string" ? raw.control : "",
      label: typeof raw.label === "string" ? raw.label : "",
      icon: typeof raw.icon === "string" ? raw.icon : "",
      missing: raw.missing === true
    };
  }

  function parseItem(raw) {
    if (raw && typeof raw === "object" && raw.type === "group") {
      var rows = Array.isArray(raw.rows) ? raw.rows.map(parseRow).filter(Boolean) : [];
      return {
        kind: "group",
        id: typeof raw.id === "string" ? raw.id : null,
        label: typeof raw.label === "string" ? raw.label : "",
        icon: typeof raw.icon === "string" ? raw.icon : "neutral",
        rows: rows
      };
    }
    return parseRow(raw);
  }

  // The catalogue as the page uses it, or null when it is not one. Anything a field holds that
  // is not of its type is dropped, never trusted.
  function parseCatalogue(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || raw.v !== 1) {
      return null;
    }
    // Editing from the receiver switched off in Home Assistant: the catalogue says that alone.
    if (raw.editing === false) {
      return {editing: false};
    }
    if (raw.editing !== undefined && raw.editing !== true) {
      return null;
    }
    if (!isRevision(raw.revision) || !Array.isArray(raw.menu) || !Array.isArray(raw.entities)) {
      return null;
    }
    var areas = Array.isArray(raw.areas) ? raw.areas.map(function (area) {
      return textOf(area, 60);
    }) : [];
    var entities = [];
    var byId = Object.create(null);
    raw.entities.forEach(function (entry) {
      if (!entry || typeof entry !== "object" || typeof entry.e !== "string") {
        return;
      }
      var controls = Array.isArray(entry.c) ? entry.c.filter(function (control) {
        return typeof control === "string";
      }) : [];
      if (!controls.length || own(byId, entry.e)) {
        return;
      }
      var area = Number.isInteger(entry.a) && entry.a >= 0 && entry.a < areas.length
        ? areas[entry.a] : "";
      var item = {
        e: entry.e,
        n: textOf(entry.n, 60) || entry.e,
        area: area,
        c: controls,
        hidden: entry.h === true,
        category: entry.k === "config" || entry.k === "diagnostic" ? entry.k : ""
      };
      item.haystack = (item.n + " " + item.e + " " + item.area).toLowerCase();
      entities.push(item);
      byId[entry.e] = item;
    });
    var leftOut = Number.isInteger(raw.left_out) && raw.left_out > 0 ? raw.left_out : 0;
    return {
      editing: true,
      revision: raw.revision,
      menu: raw.menu.map(parseItem).filter(Boolean),
      entities: entities,
      byId: byId,
      leftOut: leftOut
    };
  }

  // Search as you type: every word of the query in the name, the entity id or the area.
  function search(entities, query, limit) {
    var words = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
    var found = [];
    for (var index = 0; index < entities.length && found.length < limit; index += 1) {
      var entity = entities[index];
      var all = words.every(function (word) {
        return entity.haystack.indexOf(word) !== -1;
      });
      if (all) {
        found.push(entity);
      }
    }
    return found;
  }

  // ------------------------------------------------------------------ draft --

  function trimmed(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  // The menu as the edit carries it: the catalogue's `menu` form, ids kept, new ones null.
  function toPayload(menu) {
    function row(item) {
      var label = trimmed(item.label);
      return {
        type: "row",
        id: item.id || null,
        entity: item.entity,
        control: item.control,
        label: label ? label : null,
        icon: item.icon ? item.icon : null
      };
    }
    return menu.map(function (item) {
      if (item.kind === "group") {
        return {
          type: "group",
          id: item.id || null,
          label: trimmed(item.label),
          icon: item.icon || "neutral",
          rows: item.rows.map(row)
        };
      }
      return row(item);
    });
  }

  // Why the draft would be refused, as a text key - or null. Home Assistant checks again.
  function checkDraft(menu, limits) {
    var groups = menu.filter(function (item) { return item.kind === "group"; });
    if (groups.length > limits.groups) {
      return "limit_groups";
    }
    if (menu.length > limits.top) {
      return "limit_top";
    }
    for (var index = 0; index < menu.length; index += 1) {
      var item = menu[index];
      var rows = item.kind === "group" ? item.rows : [item];
      if (item.kind === "group") {
        if (!trimmed(item.label)) {
          return "name_group";
        }
        if (item.rows.length > limits.rows) {
          return "limit_rows";
        }
      }
      for (var inner = 0; inner < rows.length; inner += 1) {
        if (!rows[inner].entity || !rows[inner].control) {
          return "choose_entity";
        }
      }
    }
    return null;
  }

  // The sentence for a save's answer.
  function resultText(answer, texts) {
    var result = answer && typeof answer.result === "string" ? answer.result : "";
    var key = "result_" + result;
    if (result && Object.prototype.hasOwnProperty.call(texts, key)) {
      return texts[key];
    }
    return fmt(texts.result_unknown || "%s", result || "?");
  }

  // ------------------------------------------------------------------- page --

  function Creator(mount, doc, net) {
    this.mount = mount;
    this.doc = doc;
    this.net = net;
    this.texts = JSON.parse(mount.dataset.texts || "{}");
    this.icons = JSON.parse(mount.dataset.icons || "[]");
    this.limits = JSON.parse(mount.dataset.limits || "{}");
    this.catalogueUrl = mount.dataset.catalogue;
    this.saveUrl = mount.dataset.save;
    this.iconBase = mount.dataset.iconbase || "";
    this.csrf = mount.dataset.csrf;
    this.catalogue = null;
    this.menu = [];
    this.dirty = false;
    this.busy = false;
    this.message = null;
    this.offerReload = false;
    this.searches = Object.create(null);
    this.sequence = 0;
    // After a save whose catalogue is late: the revision still awaited, and polls left for it.
    this.awaited = null;
  }

  Creator.prototype.t = function (key) {
    return Object.prototype.hasOwnProperty.call(this.texts, key) ? this.texts[key] : key;
  };

  Creator.prototype.el = function (tag, text, className) {
    var node = this.doc.createElement(tag);
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    if (className) {
      node.className = className;
    }
    return node;
  };

  Creator.prototype.button = function (text, action, disabled) {
    var node = this.el("button", text);
    node.type = "button";
    node.disabled = Boolean(disabled) || this.busy;
    var self = this;
    node.addEventListener("click", function () {
      action.call(self);
    });
    return node;
  };

  Creator.prototype.say = function (message, offerReload, detail) {
    this.message = {text: message, detail: detail || null};
    this.offerReload = Boolean(offerReload);
  };

  Creator.prototype.uid = function () {
    this.sequence += 1;
    return "u" + this.sequence;
  };

  Creator.prototype.adopt = function (catalogue) {
    var self = this;
    this.catalogue = catalogue;
    this.menu = catalogue.menu.map(function (item) {
      item.uid = self.uid();
      if (item.kind === "group") {
        item.rows.forEach(function (row) { row.uid = self.uid(); });
      }
      return item;
    });
    this.dirty = false;
    this.searches = Object.create(null);
  };

  Creator.prototype.load = function () {
    var self = this;
    // A catalogue this page cannot draw is a page that says so, never one left loading.
    function failed() {
      self.catalogue = null;
      self.menu = [];
      self.say(self.t("load_failed"), true);
      try {
        self.render();
      } catch (ignored) {
        self.mount.textContent = self.t("load_failed");
      }
    }
    this.awaited = null;
    try {
      this.say(this.t("loading"));
      this.render();
    } catch (ignored) {
      failed();
      return Promise.resolve();
    }
    return this.fetchCatalogue().then(function (outcome) {
      if (outcome.catalogue) {
        self.adopt(outcome.catalogue);
        self.message = null;
        self.offerReload = false;
      } else {
        self.catalogue = null;
        self.say(self.t(outcome.reason), outcome.reason === "load_failed");
      }
      self.render();
    }).catch(failed);
  };

  // `{catalogue}` or `{reason}`: a text key that says why there is none.
  Creator.prototype.fetchCatalogue = function () {
    return this.net.fetch(this.catalogueUrl, {credentials: "same-origin", cache: "no-store"})
      .then(function (response) { return response.json(); })
      .then(function (raw) {
        if (raw && raw.catalogue === null) {
          var reason = typeof raw.reason === "string" ? raw.reason : "";
          var known = ["none", "newer", "menu_off", "not_running", "updating"];
          return {reason: known.indexOf(reason) !== -1 ? reason : "load_failed"};
        }
        var catalogue = parseCatalogue(raw);
        if (!catalogue) {
          var newer = raw && Number.isInteger(raw.v) && raw.v > 1;
          return {reason: newer ? "newer" : "load_failed"};
        }
        if (!catalogue.editing) {
          return {reason: "editing_off"};
        }
        return {catalogue: catalogue};
      })
      .catch(function () {
        return {reason: "load_failed"};
      });
  };

  // An edit. A message that offers a reload stays until the reload: it says the draft can no
  // longer be saved as it is.
  Creator.prototype.change = function () {
    this.dirty = true;
    if (this.message && !this.busy && !this.offerReload) {
      this.message = null;
    }
  };

  // ------------------------------------------------------------ the editing --

  Creator.prototype.groups = function () {
    return this.menu.filter(function (item) { return item.kind === "group"; });
  };

  Creator.prototype.containerOf = function (row) {
    if (this.menu.indexOf(row) !== -1) {
      return this.menu;
    }
    var groups = this.groups();
    for (var index = 0; index < groups.length; index += 1) {
      if (groups[index].rows.indexOf(row) !== -1) {
        return groups[index].rows;
      }
    }
    return null;
  };

  Creator.prototype.newRow = function () {
    return {kind: "row", id: null, entity: "", control: "", label: "", icon: "", missing: false,
            uid: this.uid()};
  };

  Creator.prototype.addGroup = function () {
    if (this.groups().length >= this.limits.groups || this.menu.length >= this.limits.top) {
      return;
    }
    this.menu.push({kind: "group", id: null, label: this.t("new_group"), icon: "neutral",
                    rows: [], uid: this.uid()});
    this.change();
    this.render();
  };

  Creator.prototype.addRow = function (group) {
    var container = group ? group.rows : this.menu;
    var limit = group ? this.limits.rows : this.limits.top;
    if (container.length >= limit) {
      return;
    }
    container.push(this.newRow());
    this.change();
    this.render();
  };

  Creator.prototype.move = function (container, item, step) {
    var index = container.indexOf(item);
    var target = index + step;
    if (index === -1 || target < 0 || target >= container.length) {
      return;
    }
    container.splice(index, 1);
    container.splice(target, 0, item);
    this.change();
    this.render();
  };

  Creator.prototype.remove = function (container, item) {
    var index = container.indexOf(item);
    if (index !== -1) {
      container.splice(index, 1);
      delete this.searches[item.uid];
      this.change();
      this.render();
    }
  };

  // A row to another place: "" is the top level, otherwise a group's uid.
  Creator.prototype.relocate = function (row, place) {
    var from = this.containerOf(row);
    var to = this.menu;
    if (place) {
      var group = this.groups().filter(function (item) { return item.uid === place; })[0];
      if (!group) {
        return;
      }
      to = group.rows;
    }
    if (!from || from === to) {
      return;
    }
    var limit = to === this.menu ? this.limits.top : this.limits.rows;
    if (to.length >= limit) {
      this.render();
      return;
    }
    from.splice(from.indexOf(row), 1);
    to.push(row);
    this.change();
    this.render();
  };

  Creator.prototype.choose = function (row, entity) {
    row.entity = entity.e;
    row.missing = false;
    if (entity.c.indexOf(row.control) === -1) {
      row.control = entity.c[0];
    }
    delete this.searches[row.uid];
    this.change();
    this.render();
  };

  // ---------------------------------------------------------------- saving --

  Creator.prototype.save = function () {
    var self = this;
    if (this.busy || !this.catalogue) {
      return Promise.resolve();
    }
    var refusal = checkDraft(this.menu, this.limits);
    if (refusal) {
      var bound = {limit_groups: this.limits.groups, limit_top: this.limits.top,
                   limit_rows: this.limits.rows}[refusal];
      this.say(fmt(this.t(refusal), bound));
      this.render();
      return Promise.resolve();
    }
    var base = this.catalogue.revision;
    this.awaited = null;
    var body = new this.net.URLSearchParams();
    body.append("csrf", this.csrf);
    body.append("form", "menu_edit");
    body.append("base", base);
    body.append("menu", JSON.stringify(toPayload(this.menu)));
    this.busy = true;
    this.say(this.t("saving"));
    this.render();
    return this.net.fetch(this.saveUrl, {method: "POST", credentials: "same-origin",
                                         cache: "no-store", body: body})
      .then(function (response) { return response.json(); })
      .catch(function () { return {result: "no_answer"}; })
      .then(function (answer) { return self.answered(answer, base); });
  };

  Creator.prototype.answered = function (answer, base) {
    var self = this;
    var result = answer && answer.result;
    if (result === "saved") {
      this.say(this.t("result_waiting"));
      this.render();
      var wanted = isRevision(answer.revision) ? answer.revision : null;
      return this.awaitRevision(wanted, base).then(function (outcome) {
        self.busy = false;
        if (outcome.arrived) {
          self.adopt(outcome.catalogue);
          self.say(self.t("result_saved"));
        } else {
          // The page goes on showing the menu as it was saved - never an older catalogue - and
          // takes Home Assistant's copy by itself when it comes.
          self.dirty = false;
          self.say(self.t("result_arriving"), true);
          self.following = self.follow(wanted, base);
        }
        self.render();
      });
    }
    this.busy = false;
    var detail = null;
    if (result === "invalid") {
      detail = {path: typeof answer.path === "string" ? answer.path : "",
                text: typeof answer.detail === "string" ? answer.detail : ""};
    }
    var reload = result === "stale" || result === "no_answer" || result === "out_of_date"
      || result === "unreadable";
    this.say(resultText(answer, this.texts), reload, detail);
    this.render();
    return Promise.resolve();
  };

  function carries(outcome, wanted, base) {
    var revision = outcome.catalogue ? outcome.catalogue.revision : null;
    return wanted ? revision === wanted : revision !== null && revision !== base;
  }

  // After a save: the catalogue that carries the new revision, so the next save knows the new
  // ids. At most five seconds: `{catalogue, arrived}`.
  Creator.prototype.awaitRevision = function (wanted, base) {
    var self = this;
    var deadline = this.net.now() + 5000;
    function attempt() {
      return self.fetchCatalogue().then(function (outcome) {
        var arrived = carries(outcome, wanted, base);
        if (arrived || self.net.now() >= deadline) {
          return {catalogue: outcome.catalogue || null, arrived: arrived};
        }
        return self.net.wait(500).then(attempt);
      });
    }
    return attempt();
  };

  // Then, every two seconds for another minute: the catalogue is taken when it carries the new
  // revision, unless the page has been edited, saved or reloaded since - an edit is never thrown
  // away for it. After that minute the page says no more than it does.
  Creator.prototype.follow = function (wanted, base) {
    var self = this;
    var token = {left: 30};
    this.awaited = token;
    function attempt() {
      if (self.awaited !== token) {
        return Promise.resolve();
      }
      if (token.left <= 0) {
        self.awaited = null;
        return Promise.resolve();
      }
      token.left -= 1;
      return self.net.wait(2000).then(function () {
        if (self.awaited !== token) {
          return null;
        }
        return self.fetchCatalogue();
      }).then(function (outcome) {
        if (!outcome || self.awaited !== token) {
          return null;
        }
        if (!carries(outcome, wanted, base)) {
          return attempt();
        }
        self.awaited = null;
        if (!self.dirty && !self.busy) {
          self.adopt(outcome.catalogue);
          self.say(self.t("result_saved"));
          self.render();
        }
        return null;
      });
    }
    return attempt();
  };

  Creator.prototype.reload = function () {
    if (this.busy) {
      return Promise.resolve();
    }
    // The draft stays on the page until the user agrees to lose it.
    if (this.dirty && !this.net.confirm(this.t("confirm_reload"))) {
      return Promise.resolve();
    }
    if (this.message && this.message.text === this.t("result_out_of_date")) {
      this.net.reloadPage();
      return Promise.resolve();
    }
    return this.load();
  };

  // --------------------------------------------------------------- drawing --

  Creator.prototype.select = function (options, current, onChange) {
    var node = this.el("select");
    var self = this;
    options.forEach(function (option) {
      var item = self.el("option", option[1]);
      item.value = option[0];
      if (option[2]) {
        item.disabled = true;
      }
      if (option[0] === current) {
        item.selected = true;
      }
      node.appendChild(item);
    });
    node.disabled = this.busy;
    node.addEventListener("change", function () { onChange.call(self, node.value); });
    return node;
  };

  Creator.prototype.field = function (label, control) {
    var node = this.el("label");
    node.appendChild(this.el("span", label));
    node.appendChild(control);
    return node;
  };

  Creator.prototype.textInput = function (value, onInput) {
    var node = this.el("input");
    node.type = "text";
    node.maxLength = this.limits.text;
    node.value = value;
    node.disabled = this.busy;
    var self = this;
    node.addEventListener("input", function () { onInput.call(self, node.value); });
    return node;
  };

  // The address of an icon's picture: only for a name of the page's own list, which the
  // receiver serves; any other name - one from the catalogue - never reaches an address.
  Creator.prototype.iconUrl = function (name) {
    return this.icons.indexOf(name) !== -1 ? this.iconBase + name + ".png" : "";
  };

  // The icon's picture with its name beside it, and the list of names to choose from.
  Creator.prototype.iconSelect = function (current, withDefault, onChange) {
    var self = this;
    var options = withDefault ? [["", this.t("default")]] : [];
    this.icons.forEach(function (name) { options.push([name, name]); });
    var wrap = this.el("span", null, "icon");
    var picture = this.el("img");
    picture.alt = "";
    picture.width = 32;
    picture.height = 32;
    function show(name) {
      // A group always has an icon: one the list does not name is drawn as the neutral one.
      var address = self.iconUrl(name) || (withDefault ? "" : self.iconUrl("neutral"));
      picture.src = address;
      picture.hidden = !address;
    }
    show(current);
    wrap.appendChild(picture);
    wrap.appendChild(this.select(options, current, function (value) {
      show(value);
      onChange.call(self, value);
    }));
    return wrap;
  };

  Creator.prototype.tools = function (container, item) {
    var node = this.el("div", null, "tools");
    var index = container.indexOf(item);
    node.appendChild(this.button(this.t("up"), function () {
      this.move(container, item, -1);
    }, index <= 0));
    node.appendChild(this.button(this.t("down"), function () {
      this.move(container, item, 1);
    }, index >= container.length - 1));
    node.appendChild(this.button(this.t("remove"), function () {
      this.remove(container, item);
    }));
    return node;
  };

  Creator.prototype.entityLine = function (row) {
    var entity = own(this.catalogue.byId, row.entity);
    var node = this.el("div", null, "meta");
    if (!row.entity) {
      return node;
    }
    if (row.missing) {
      node.appendChild(this.el("span", fmt(this.t("missing"), row.entity), "bad"));
      return node;
    }
    if (!entity) {
      node.appendChild(this.el("span", fmt(this.t("not_listed"), row.entity), "bad"));
      return node;
    }
    node.appendChild(this.el("strong", entity.n));
    node.appendChild(this.el("span", " " + this.describe(entity)));
    return node;
  };

  Creator.prototype.describe = function (entity) {
    var parts = [entity.e];
    if (entity.area) {
      parts.push(entity.area);
    }
    if (entity.hidden) {
      parts.push(this.t("hidden"));
    }
    if (entity.category) {
      parts.push(this.t(entity.category));
    }
    return parts.join(" - ");
  };

  Creator.prototype.picker = function (row) {
    var self = this;
    var wrap = this.el("div");
    var input = this.el("input");
    input.type = "search";
    input.placeholder = this.t("search");
    input.disabled = this.busy;
    var state = this.searches[row.uid] || {query: "", active: 0};
    input.value = state.query;
    var list = this.el("div", null, "pick");
    function draw() {
      while (list.firstChild) {
        list.removeChild(list.firstChild);
      }
      if (!state.query) {
        return;
      }
      var found = search(self.catalogue.entities, state.query, 50);
      if (!found.length) {
        list.appendChild(self.el("p", self.t("no_matches"), "meta"));
        return;
      }
      state.active = Math.min(state.active, found.length - 1);
      found.forEach(function (entity, index) {
        var option = self.el("button", null, index === state.active ? "active" : "");
        option.type = "button";
        option.appendChild(self.el("strong", entity.n));
        option.appendChild(self.el("span", " " + self.describe(entity), "meta"));
        option.addEventListener("click", function () { self.choose(row, entity); });
        list.appendChild(option);
      });
      state.found = found;
    }
    input.addEventListener("input", function () {
      state.query = input.value;
      state.active = 0;
      self.searches[row.uid] = state;
      draw();
    });
    input.addEventListener("keydown", function (event) {
      var found = state.found || [];
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        var step = event.key === "ArrowDown" ? 1 : -1;
        state.active = Math.max(0, Math.min(found.length - 1, state.active + step));
        draw();
      } else if (event.key === "Enter") {
        event.preventDefault();
        if (found[state.active]) {
          self.choose(row, found[state.active]);
        }
      } else if (event.key === "Escape") {
        state.query = "";
        input.value = "";
        draw();
      }
    });
    draw();
    wrap.appendChild(input);
    wrap.appendChild(list);
    return wrap;
  };

  Creator.prototype.controlOptions = function (row) {
    var entity = own(this.catalogue.byId, row.entity);
    var allowed = entity && !row.missing ? entity.c : (row.control ? [row.control] : []);
    var self = this;
    return allowed.map(function (control) {
      var key = "control_" + control;
      var known = CONTROLS.indexOf(control) !== -1;
      return [control, known ? self.t(key) : control];
    });
  };

  Creator.prototype.placeOptions = function (row) {
    var self = this;
    var from = this.containerOf(row);
    var options = [["", this.t("top"), from !== this.menu && this.menu.length >= this.limits.top]];
    this.groups().forEach(function (group, index) {
      var name = trimmed(group.label) || self.t("group") + " " + (index + 1);
      var full = from !== group.rows && group.rows.length >= self.limits.rows;
      options.push([group.uid, name, full]);
    });
    return options;
  };

  Creator.prototype.drawRow = function (container, row, group) {
    var node = this.el("div", null, "item row");
    node.appendChild(this.entityLine(row));
    node.appendChild(this.field(this.t("entity"), this.picker(row)));
    node.appendChild(this.field(this.t("control"), this.select(this.controlOptions(row),
      row.control, function (value) { row.control = value; this.change(); })));
    node.appendChild(this.field(this.t("label"), this.textInput(row.label, function (value) {
      row.label = value;
      this.change();
    })));
    node.appendChild(this.field(this.t("icon"), this.iconSelect(row.icon, true, function (value) {
      row.icon = value;
      this.change();
    })));
    node.appendChild(this.field(this.t("place"), this.select(this.placeOptions(row),
      group ? group.uid : "", function (value) { this.relocate(row, value); })));
    node.appendChild(this.tools(container, row));
    return node;
  };

  Creator.prototype.drawGroup = function (group) {
    var self = this;
    var node = this.el("div", null, "item group");
    node.appendChild(this.el("strong", this.t("group")));
    node.appendChild(this.field(this.t("label"), this.textInput(group.label, function (value) {
      group.label = value;
      this.change();
    })));
    node.appendChild(this.field(this.t("icon"), this.iconSelect(group.icon, false,
      function (value) { group.icon = value; this.change(); })));
    node.appendChild(this.tools(this.menu, group));
    if (!group.rows.length) {
      node.appendChild(this.el("p", this.t("empty_group"), "meta"));
    }
    group.rows.forEach(function (row) {
      node.appendChild(self.drawRow(group.rows, row, group));
    });
    var full = group.rows.length >= this.limits.rows;
    node.appendChild(this.button(this.t("add_row"), function () { this.addRow(group); }, full));
    if (full) {
      node.appendChild(this.el("p", fmt(this.t("limit_rows"), this.limits.rows), "meta"));
    }
    return node;
  };

  Creator.prototype.drawMessage = function () {
    var node = this.el("div", null, "notice");
    node.setAttribute("role", "status");
    if (!this.message) {
      return node;
    }
    node.appendChild(this.el("p", this.message.text));
    var detail = this.message.detail;
    if (detail && (detail.text || detail.path)) {
      var box = this.el("p", null, "meta");
      box.appendChild(this.el("strong", this.t("ha_says") + " "));
      box.appendChild(this.el("span", detail.text));
      if (detail.path) {
        box.appendChild(this.el("span", " " + this.t("where") + " " + detail.path));
      }
      node.appendChild(box);
    }
    if (this.offerReload) {
      node.appendChild(this.button(this.t("reload"), function () { this.reload(); }));
    }
    return node;
  };

  Creator.prototype.render = function () {
    var self = this;
    var mount = this.mount;
    while (mount.firstChild) {
      mount.removeChild(mount.firstChild);
    }
    mount.appendChild(this.drawMessage());
    if (!this.catalogue) {
      return;
    }
    if (this.catalogue.leftOut) {
      mount.appendChild(this.el("p", fmt(this.t("left_out"), this.catalogue.leftOut), "meta"));
    }
    if (!this.menu.length) {
      mount.appendChild(this.el("p", this.t("empty_menu"), "meta"));
    }
    this.menu.forEach(function (item) {
      mount.appendChild(item.kind === "group" ? self.drawGroup(item)
        : self.drawRow(self.menu, item, null));
    });
    var bar = this.el("div", null, "bar");
    var topFull = this.menu.length >= this.limits.top;
    var groupsFull = this.groups().length >= this.limits.groups;
    bar.appendChild(this.button(this.t("add_group"), this.addGroup, topFull || groupsFull));
    bar.appendChild(this.button(this.t("add_row"), function () { this.addRow(null); }, topFull));
    bar.appendChild(this.button(this.t("save"), this.save, false));
    if (this.dirty) {
      bar.appendChild(this.el("span", this.t("unsaved"), "meta"));
    }
    if (groupsFull) {
      bar.appendChild(this.el("span", fmt(this.t("limit_groups"), this.limits.groups), "meta"));
    } else if (topFull) {
      bar.appendChild(this.el("span", fmt(this.t("limit_top"), this.limits.top), "meta"));
    }
    mount.appendChild(bar);
  };

  // ---------------------------------------------------------------- start --

  function start(win) {
    var doc = win.document;
    var mount = doc.getElementById("creator");
    if (!mount) {
      return null;
    }
    var net = {
      fetch: function (url, options) { return win.fetch(url, options); },
      URLSearchParams: win.URLSearchParams,
      now: function () { return Date.now(); },
      wait: function (milliseconds) {
        return new Promise(function (resolve) { win.setTimeout(resolve, milliseconds); });
      },
      reloadPage: function () { win.location.reload(); },
      confirm: function (text) { return win.confirm(text); }
    };
    var creator = new Creator(mount, doc, net);
    win.addEventListener("beforeunload", function (event) {
      if (creator.dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    });
    creator.load();
    return creator;
  }

  var exported = {
    Creator: Creator,
    checkDraft: checkDraft,
    fmt: fmt,
    own: own,
    parseCatalogue: parseCatalogue,
    resultText: resultText,
    search: search,
    start: start,
    toPayload: toPayload
  };
  if (typeof module === "object" && module && module.exports) {
    module.exports = exported;
  } else if (root && root.document) {
    start(root);
  }
}(typeof window === "undefined" ? null : window));
