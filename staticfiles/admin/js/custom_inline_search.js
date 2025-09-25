document.addEventListener("DOMContentLoaded", function() {
    const inlineGroups = document.querySelectorAll(".inline-group");

    inlineGroups.forEach(group => {
        let header = group.querySelector("h2");
        if (!header) return;
        if (header.querySelector(".sn-search-btn")) return;

        let inputs = Array.from(group.children)
            .filter(child => child.matches(".inline-related"))
            .flatMap(child => Array.from(child.querySelectorAll("input[name$='-seriove_cislo']")));

        if (inputs.length === 0) {
            let btn = document.createElement("button");
            btn.innerText = "🔍 Vyhľadať kategóriu produktu";
            btn.type = "button";
            btn.classList.add("button", "sn-search-btn");
            btn.style.float = "right";
            btn.style.marginRight = "10px";

            btn.addEventListener("click", () => {
                let subGroups = group.querySelectorAll(".inline-related h2");
                if (subGroups.length === 0) {
                    alert("Táto sekcia nemá žiadne podmodely.");
                    return;
                }

                let modal = document.createElement("div");
                modal.style.position = "fixed";
                modal.style.top = "0";
                modal.style.left = "0";
                modal.style.width = "100%";
                modal.style.height = "100%";
                modal.style.backgroundColor = "rgba(0,0,0,0.5)";
                modal.style.display = "flex";
                modal.style.alignItems = "center";
                modal.style.justifyContent = "center";
                modal.style.zIndex = "10000";

                let box = document.createElement("div");
                box.style.background = "white";
                box.style.padding = "20px";
                box.style.borderRadius = "8px";
                box.style.minWidth = "300px";
                box.style.boxShadow = "0 4px 8px rgba(0,0,0,0.3)";
                modal.appendChild(box);

                let label = document.createElement("div");
                label.innerText = "Vyberte kategóriu:";
                label.style.marginBottom = "10px";
                label.style.fontWeight = "bold";
                box.appendChild(label);

                let select = document.createElement("select");
                select.style.width = "100%";
                select.style.padding = "6px";
                select.style.marginBottom = "15px";
                select.style.fontSize = "14px";

                let defaultOption = document.createElement("option");
                defaultOption.innerText = "-- Vyberte --";
                defaultOption.value = "";
                select.appendChild(defaultOption);

                let names = Array.from(subGroups).map(h => {
                    let clean = h.innerText.trim();
                    clean = clean.replace(/🔍.*$/, "").trim();
                    return clean.charAt(0).toUpperCase() + clean.slice(1).toLowerCase();
                });
                names = [...new Set(names)];
                names.forEach(name => {
                    let opt = document.createElement("option");
                    opt.value = name;
                    opt.innerText = name;
                    select.appendChild(opt);
                });

                box.appendChild(select);

                let btnRow = document.createElement("div");
                btnRow.style.display = "flex";
                btnRow.style.justifyContent = "flex-end";
                btnRow.style.gap = "10px";

                let closeBtn = document.createElement("button");
                closeBtn.innerText = "Zavrieť";
                closeBtn.type = "button";
                closeBtn.style.padding = "6px 12px";
                closeBtn.style.cursor = "pointer";

                let okBtn = document.createElement("button");
                okBtn.innerText = "Vybrať";
                okBtn.type = "button";
                okBtn.style.padding = "6px 12px";
                okBtn.style.background = "#2b7cff";
                okBtn.style.color = "white";
                okBtn.style.border = "none";
                okBtn.style.cursor = "pointer";
                okBtn.style.borderRadius = "4px";

                btnRow.appendChild(closeBtn);
                btnRow.appendChild(okBtn);
                box.appendChild(btnRow);

                document.body.appendChild(modal);

                function closeModal() {
                    document.body.removeChild(modal);
                }

                closeBtn.addEventListener("click", closeModal);
                modal.addEventListener("click", e => { if (e.target === modal) closeModal(); });
                document.addEventListener("keydown", function escHandler(e) {
                    if (e.key === "Escape") {
                        closeModal();
                        document.removeEventListener("keydown", escHandler);
                    }
                });

                okBtn.addEventListener("click", () => {
                    if (!select.value) return;
                    let chosen = Array.from(subGroups).find(h => {
                        let clean = h.innerText.trim().replace(/🔍.*$/, "").trim();
                        clean = clean.charAt(0).toUpperCase() + clean.slice(1).toLowerCase();
                        return clean === select.value;
                    });
                    if (chosen) {
                        chosen.scrollIntoView({behavior: "smooth", block: "center"});
                        chosen.style.backgroundColor = "#fff8dc";
                        chosen.style.border = "2px solid red";
                        setTimeout(() => {
                            chosen.style.backgroundColor = "";
                            chosen.style.border = "";
                        }, 3000);
                    }
                    closeModal();
                });
            });

            header.appendChild(btn);
            return;
        }

        let btn = document.createElement("button");
        btn.innerText = "🔍 Vyhľadať podľa sériového čísla produktu";
        btn.type = "button";
        btn.classList.add("button", "sn-search-btn");
        btn.style.float = "right";
        btn.style.marginRight = "10px";

        btn.addEventListener("click", () => {
            let sn = prompt("Zadajte sériové číslo:");
            if (!sn) return;
            let found = false;

            inputs.forEach(input => {
                if (input.value.trim() === sn.trim()) {
                    input.scrollIntoView({behavior: "smooth", block: "center"});
                    input.style.border = "2px solid red";
                    input.style.backgroundColor = "#fff8dc";
                    setTimeout(() => {
                        input.style.border = "";
                        input.style.backgroundColor = "";
                    }, 3000);
                    found = true;
                }
            });

            if (!found) {
                alert("Sériové číslo " + sn + " sa v tomto podmodule nenašlo.");
            }
        });

        header.appendChild(btn);
    });
});

document.addEventListener("DOMContentLoaded", function() {
    function scrollToGroup(titleText) {
        const groups = document.querySelectorAll(".inline-group h2");
        groups.forEach(h2 => {
            if (h2.textContent.trim().toUpperCase().includes(titleText.toUpperCase())) {
                h2.scrollIntoView({behavior: "smooth", block: "center"});
            }
        });
    }

    if (!document.querySelector("#id_nazov")) {
        return;
    }

    const fieldset = document.querySelector("#id_nazov")?.closest("fieldset");
    if (fieldset && !document.querySelector(".global-search-wrapper")) {
        let wrapper = document.createElement("div");
        wrapper.classList.add("global-search-wrapper");
        wrapper.style.margin = "15px 0";
        wrapper.style.display = "flex";
        wrapper.style.gap = "10px";

        function createButton(label, color, target) {
            let btn = document.createElement("button");
            btn.innerText = "🔍 " + label;
            btn.type = "button";
            btn.style.backgroundColor = color;
            btn.style.color = "white";
            btn.style.fontSize = "16px";
            btn.style.fontWeight = "bold";
            btn.style.padding = "10px 20px";
            btn.style.border = "none";
            btn.style.borderRadius = "6px";
            btn.style.cursor = "pointer";
            btn.style.boxShadow = "0 2px 6px rgba(0,0,0,0.2)";
            btn.addEventListener("mouseover", () => {
                btn.style.opacity = "0.9";
            });
            btn.addEventListener("mouseout", () => {
                btn.style.opacity = "1";
            });
            btn.addEventListener("click", () => {
                scrollToGroup(target);
            });
            return btn;
        }

        wrapper.appendChild(createButton("Stroje", "#28a745", "STROJE"));
        wrapper.appendChild(createButton("Spotrebné diely", "#17a2b8", "SPOTREBNÉ NÁHRADNÉ DIELY"));
        wrapper.appendChild(createButton("Poruchové diely", "#ffc107", "PORUCHOVÉ DIELY"));

        fieldset.insertAdjacentElement("afterend", wrapper);
    }
});

document.addEventListener("DOMContentLoaded", function() {
    const headers = document.querySelectorAll(".inline-group h2");

    headers.forEach(h2 => {
        let text = h2.textContent.trim().toUpperCase();

        if (text.includes("STROJE")) {
            h2.style.backgroundColor = "#28a745";
            h2.style.color = "white";
            h2.style.padding = "6px 10px";
            h2.style.borderRadius = "4px";
        }

        if (text.includes("SPOTREBNÉ NÁHRADNÉ DIELY")) {
            h2.style.backgroundColor = "#17a2b8";
            h2.style.color = "white";
            h2.style.padding = "6px 10px";
            h2.style.borderRadius = "4px";
        }

        if (text.includes("PORUCHOVÉ DIELY")) {
            h2.style.backgroundColor = "#ffc107";
            h2.style.color = "black";
            h2.style.padding = "6px 10px";
            h2.style.borderRadius = "4px";
        }
    });
});

document.addEventListener("DOMContentLoaded", function() {
    const headers = document.querySelectorAll(".inline-group");

    headers.forEach(group => {
        const h2 = group.querySelector("h2");
        if (!h2) return;

        let color = "";
        if (h2.textContent.toUpperCase().includes("STROJE")) color = "#28a745";  
        if (h2.textContent.toUpperCase().includes("SPOTREBNÉ NÁHRADNÉ DIELY")) color = "#17a2b8";  
        if (h2.textContent.toUpperCase().includes("PORUCHOVÉ DIELY")) color = "#ffc107";  

        const buttons = group.querySelectorAll(".sn-search-btn");

        buttons.forEach(btn => {
            if (btn.innerText.includes("kategóriu")) {
                btn.style.backgroundColor = color;
                btn.style.color = (color === "#ffc107") ? "black" : "white";
                btn.style.fontSize = "13px";
                btn.style.padding = "5px 14px";
                btn.style.border = "none";
                btn.style.borderRadius = "6px";
                btn.style.cursor = "pointer";
                btn.style.boxShadow = "0 2px 4px rgba(0,0,0,0.15)";
                btn.style.marginLeft = "8px";
            } else {
                btn.style.backgroundColor = "white";
                btn.style.color = color;
                btn.style.fontSize = "13px";
                btn.style.padding = "5px 14px";
                btn.style.border = "1px solid " + color;
                btn.style.borderRadius = "6px";
                btn.style.cursor = "pointer";
                btn.style.marginLeft = "8px";
                btn.style.transition = "all 0.2s ease-in-out";
            }
        });
    });
});
