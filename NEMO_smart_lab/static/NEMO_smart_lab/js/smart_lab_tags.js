/*
 * A minimal multi-select "tag" text input (type to search a suggestion list, Enter/comma/click
 * to add a tag, backspace on an empty box to remove the last one) - used by tool_history.html's
 * recipe/user run-history filters. No vendored library: this is small and specific enough
 * (two inputs, one page) that a bespoke ~100-line widget is simpler than pulling in a whole
 * tagging/autocomplete dependency for it - consistent with this project's "vendor exactly what's
 * used" approach for genuinely reusable pieces (uPlot, Chart.js) versus one-off UI like this.
 *
 * Renders one hidden <input type="hidden" name="{name}" value="{tag}"> per selected tag inside
 * the given root element, so a plain <form method="get"> submits every tag as a repeated query
 * param (?recipe=A&recipe=B) with no extra JS needed on submit - the visible text box itself is
 * never submitted (it has no "name" attribute).
 */
(function () {
    function smartLabInitTagInput(root, options) {
        var name = options.name;
        var suggestions = options.suggestions || [];
        var initial = options.initial || [];
        var placeholder = options.placeholder || "";

        var tags = [];
        var filtered = [];
        var activeIndex = -1;

        root.innerHTML = "";
        root.style.position = "relative";

        var chipsWrap = document.createElement("div");
        chipsWrap.style.cssText =
            "display:flex;flex-wrap:wrap;gap:4px;align-items:center;border:1px solid #ccc;" +
            "border-radius:4px;padding:4px 6px;background:#fff;min-height:32px;cursor:text";
        chipsWrap.addEventListener("click", function () {
            input.focus();
        });

        var input = document.createElement("input");
        input.type = "text";
        input.autocomplete = "off";
        input.placeholder = placeholder;
        input.style.cssText = "border:none;outline:none;flex:1 1 100px;min-width:100px;font-size:inherit;padding:2px";

        var menu = document.createElement("ul");
        menu.className = "dropdown-menu";
        menu.style.cssText = "display:none;position:absolute;top:100%;left:0;right:0;max-height:220px;overflow-y:auto;z-index:1000";

        function hideMenu() {
            menu.style.display = "none";
            menu.innerHTML = "";
            activeIndex = -1;
        }

        function highlightActive() {
            for (var i = 0; i < menu.children.length; i++) {
                menu.children[i].className = i === activeIndex ? "active" : "";
            }
        }

        function showSuggestions() {
            var query = input.value.trim().toLowerCase();
            filtered = suggestions.filter(function (s) {
                if (tags.some(function (t) { return t.toLowerCase() === s.toLowerCase(); })) return false;
                return !query || s.toLowerCase().indexOf(query) !== -1;
            }).slice(0, 15);
            menu.innerHTML = "";
            if (!filtered.length) {
                hideMenu();
                return;
            }
            filtered.forEach(function (s) {
                var li = document.createElement("li");
                var a = document.createElement("a");
                a.href = "#";
                a.textContent = s;
                // mousedown (not click) fires before the text input's blur, so a suggestion click
                // still registers instead of being lost to blur's hideMenu.
                a.addEventListener("mousedown", function (e) {
                    e.preventDefault();
                    addTag(s);
                });
                li.appendChild(a);
                menu.appendChild(li);
            });
            activeIndex = -1;
            menu.style.display = "block";
        }

        function renderChips() {
            var existing = chipsWrap.querySelectorAll("[data-smart-lab-chip]");
            for (var i = 0; i < existing.length; i++) existing[i].remove();
            tags.forEach(function (tag) {
                var chip = document.createElement("span");
                chip.className = "label label-default";
                chip.setAttribute("data-smart-lab-chip", "1");
                chip.style.cssText = "display:inline-flex;align-items:center;font-size:90%;padding:4px 6px";
                chip.appendChild(document.createTextNode(tag));

                var remove = document.createElement("a");
                remove.href = "#";
                remove.style.cssText = "color:inherit;margin-left:5px;text-decoration:none;font-weight:bold";
                remove.innerHTML = "&times;";
                remove.addEventListener("click", function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    removeTag(tag);
                });
                chip.appendChild(remove);

                var hidden = document.createElement("input");
                hidden.type = "hidden";
                hidden.name = name;
                hidden.value = tag;
                chip.appendChild(hidden);

                chipsWrap.insertBefore(chip, input);
            });
        }

        function addTag(value) {
            value = (value || "").trim();
            if (!value) return;
            var exists = tags.some(function (t) { return t.toLowerCase() === value.toLowerCase(); });
            if (!exists) {
                tags.push(value);
                renderChips();
            }
            input.value = "";
            hideMenu();
        }

        function removeTag(value) {
            tags = tags.filter(function (t) { return t !== value; });
            renderChips();
            input.focus();
        }

        input.addEventListener("input", showSuggestions);
        input.addEventListener("focus", showSuggestions);
        input.addEventListener("blur", function () {
            // Delayed so a suggestion's mousedown (above) still fires first.
            setTimeout(hideMenu, 150);
        });
        input.addEventListener("keydown", function (e) {
            if (e.key === "ArrowDown") {
                e.preventDefault();
                if (!filtered.length) return;
                activeIndex = Math.min(activeIndex + 1, filtered.length - 1);
                highlightActive();
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                if (!filtered.length) return;
                activeIndex = Math.max(activeIndex - 1, 0);
                highlightActive();
            } else if (e.key === "Enter" || e.key === ",") {
                e.preventDefault();
                if (activeIndex >= 0 && filtered[activeIndex]) {
                    addTag(filtered[activeIndex]);
                } else if (input.value.trim()) {
                    addTag(input.value);
                }
            } else if (e.key === "Backspace" && !input.value && tags.length) {
                removeTag(tags[tags.length - 1]);
            } else if (e.key === "Escape") {
                hideMenu();
            }
        });

        chipsWrap.appendChild(input);
        root.appendChild(chipsWrap);
        root.appendChild(menu);

        initial.forEach(function (v) {
            if (v && tags.indexOf(v) === -1) tags.push(v);
        });
        renderChips();
    }

    window.smartLabInitTagInput = smartLabInitTagInput;
})();
