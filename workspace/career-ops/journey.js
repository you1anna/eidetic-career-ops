(() => {
  "use strict";
  const stages = [
    {
      "name": "Direction",
      "title": "Set the type of work you are looking for.",
      "description": "Alex wants an operations role in a growing service team. They record their relevant experience, pay expectations and location preferences.",
      "input": "Relevant experience, projects you can talk about, and your preferences on pay, location and working pattern.",
      "work": "Compares roles with the experience, pay expectations and location preferences Alex has recorded.",
      "result": "Recorded criteria to compare with the duties and terms of each role.",
      "exampleTitle": "What matters to Alex",
      "rows": [
        [
          "The work",
          "Running day-to-day operations and helping a growing service team work well."
        ],
        [
          "Experience to draw on",
          "Led a service handover and improved onboarding across two teams."
        ],
        [
          "Location and working pattern",
          "Hybrid in London or the South East, four or five days a week."
        ]
      ],
      "choice": "You set the direction and correct anything that does not reflect your experience.",
      "adviserTitle": "Agree the direction together",
      "adviserRows": [
        [
          "What Alex shares",
          "The kind of work they want and two project examples to talk through."
        ],
        [
          "How the adviser helps",
          "Tests the focus and helps Alex describe what they can bring to a team."
        ],
        [
          "Agreed next step",
          "Alex settles on the work and terms that matter before comparing opportunities."
        ]
      ],
      "adviserChoice": "Alex decides which experience and preferences to share.",
      "adviserNote": "In the proposed service, the adviser sees only the context Alex chooses to share."
    },
    {
      "name": "Discover",
      "title": "Compare a small number of relevant roles.",
      "description": "Alex finds an operations role at Northbank Studio among a small set of candidates. Their existing applications stay separate, so new discoveries do not bury work already under way.",
      "input": "Employer adverts, forwarded job alerts, public requests for work and relevant company updates.",
      "work": "Brings the sources together, checks suitability and keeps up to ten unchosen candidates in the working list.",
      "result": "Up to ten opportunities under consideration, separate from applications already started.",
      "exampleTitle": "Roles under consideration",
      "rows": [
        [
          "Worth a closer look",
          "Northbank Studio’s operations role has a clear remit and relevant experience to compare."
        ],
        [
          "Still being checked",
          "Another promising role needs firmer evidence of its working pattern before Alex can weigh it up."
        ],
        [
          "Kept separately",
          "An application Alex has already started stays in active work. Older leads remain searchable."
        ]
      ],
      "choice": "Choose what to pursue. You can leave the reference collection alone.",
      "adviserTitle": "Compare the roles with an adviser",
      "adviserRows": [
        [
          "What Alex shares",
          "Two promising roles and the question that makes it hard to choose between them."
        ],
        [
          "How the adviser helps",
          "Compares the work and practical tradeoffs with Alex’s priorities."
        ],
        [
          "Agreed next step",
          "Alex takes a closer look at Northbank and leaves the weaker match aside."
        ]
      ],
      "adviserChoice": "A short shared list keeps the conversation focused; Alex keeps control of the wider search.",
      "adviserNote": "The proposed adviser view helps compare selected opportunities before the next session."
    },
    {
      "name": "Assess",
      "title": "Compare the role with your experience and requirements.",
      "description": "Northbank’s advert describes service handovers and onboarding. Alex has experience in both. The assessment compares that experience with the role and identifies a missing detail about office attendance.",
      "input": "The dated advert, your recorded experience, and any other evidence about the role.",
      "work": "Assesses the actual work and requirements, compares the terms and explains why the opportunity deserves attention.",
      "result": "A comparison of the role’s requirements with Alex’s experience, including the terms still to confirm.",
      "exampleTitle": "Northbank: worth a closer look",
      "label": "Fictional opportunity assessment",
      "rows": [
        [
          "The work",
          "Own service handovers and improve onboarding. Employer advert, checked 29 October."
        ],
        [
          "Why it fits",
          "Alex has led a service handover and improved onboarding across two teams."
        ],
        [
          "Terms to compare",
          "Permanent, London, hybrid. The salary range meets Alex’s recorded minimum."
        ],
        [
          "Still to check",
          "The advert does not say how many days are required in the office. Alex wants to ask before applying."
        ]
      ],
      "choice": "Alex can check the source, shortlist the role and ask about office days before applying.",
      "adviserTitle": "Help Alex weigh the evidence",
      "adviserRows": [
        [
          "What Alex shares",
          "The role, the reasons it looks suitable and the question about office days."
        ],
        [
          "How the adviser helps",
          "Checks the match against Alex’s experience and discusses the practical tradeoff."
        ],
        [
          "Agreed next step",
          "Alex shortlists the role and asks about the working pattern before committing more time."
        ]
      ],
      "adviserChoice": "The adviser helps make sense of the evidence; the decision stays with Alex.",
      "adviserNote": "The proposed adviser experience uses the same evidence and unknowns Alex chooses to share."
    },
    {
      "name": "Connect",
      "title": "Contact someone who knows the company.",
      "description": "Alex remembers Sam, a former colleague who knows Northbank. Alex can ask about the team and working arrangements before deciding whether to apply.",
      "input": "The opportunity brief, relevant contacts and your notes on how you know them.",
      "work": "Shows the notes about Sam alongside the role and its source, so Alex can decide what to ask.",
      "result": "The role details, previous contact notes and a question Alex wants answered.",
      "exampleTitle": "A question for Sam",
      "rows": [
        [
          "How they know each other",
          "Alex and Sam worked together on a service handover."
        ],
        [
          "What Alex wants to know",
          "What is Northbank’s operations team like, and how does hybrid working work in practice?"
        ],
        [
          "The route Alex chooses",
          "Ask Sam about the team, then apply through the employer’s website if the role still fits."
        ]
      ],
      "choice": "Write and send in your usual email or messaging tool. You choose who to contact and when.",
      "adviserTitle": "Discuss what to ask Sam",
      "adviserRows": [
        [
          "What Alex shares",
          "The question about office days and how Alex knows Sam."
        ],
        [
          "How the adviser helps",
          "Helps Alex make a clear, comfortable ask without assuming Sam can arrange an introduction."
        ],
        [
          "Agreed next step",
          "Alex writes to Sam in their own words and keeps the employer’s application link handy."
        ]
      ],
      "adviserChoice": "Alex writes and sends the message; sharing context is their choice.",
      "adviserNote": "The proposed adviser view includes selected context, without access to private message threads."
    },
    {
      "name": "Continue",
      "title": "Keep track of the application.",
      "description": "Sam replies and Alex applies through Northbank’s website. career/ops keeps the recorded progress and next step together, so Alex can return without reconstructing the story.",
      "input": "An optional note about what happened and any next step you have agreed.",
      "work": "Records the application stage, whom Alex is waiting for and any agreed action.",
      "result": "The application stage, what happened last and the next agreed action.",
      "exampleTitle": "Application status and next action",
      "rows": [
        [
          "Sam’s reply",
          "Two days a week in the office sounds workable to Alex; the employer still needs to confirm it."
        ],
        [
          "Application under way",
          "Alex records the application after submitting it on Northbank’s website."
        ],
        [
          "What happens next",
          "The team has invited Alex to a conversation on Thursday. Alex records the agreed next step."
        ]
      ],
      "choice": "Record what helps you. Your email stays in your own tools, and you choose when to follow up.",
      "adviserTitle": "Help Alex prepare for the next conversation",
      "adviserRows": [
        [
          "What Alex shares",
          "The application stage and the agreed conversation with Northbank."
        ],
        [
          "How the adviser helps",
          "Works through the experience Alex wants to discuss and the terms still to confirm."
        ],
        [
          "Agreed next step",
          "Alex has the conversation and records anything useful for the next decision."
        ]
      ],
      "adviserChoice": "Sharing progress does not require sharing the whole conversation.",
      "adviserNote": "The proposed adviser view gives sessions a starting point from the progress Alex shares."
    },
    {
      "name": "Review",
      "title": "Review the outcome and update the search.",
      "description": "The conversation helps Alex understand Northbank’s remit and working pattern. Alex updates the next step and decides which other opportunities still deserve attention.",
      "input": "What you learned, the outcome you recorded and any change in what you want.",
      "work": "Keeps previous assessments and recorded outcomes available when Alex reviews the search.",
      "result": "A record of what Alex learned and any changes they choose to make to the search.",
      "exampleTitle": "Alex decides what comes next",
      "rows": [
        [
          "What became clearer",
          "Northbank confirms the office days and explains how much of the role involves service handovers."
        ],
        [
          "What Alex decides",
          "Continue the process and keep one other suitable role shortlisted."
        ],
        [
          "What stays available",
          "Earlier opportunities and conversation notes remain in the history without filling the working list."
        ]
      ],
      "choice": "You decide whether to change direction. Your notes help you reflect on what happened.",
      "adviserTitle": "Review the results with an adviser",
      "adviserRows": [
        [
          "What Alex shares",
          "The outcome of the conversation and the choice they are considering next."
        ],
        [
          "How the adviser helps",
          "Reviews which opportunities were useful and where Alex needs more support."
        ],
        [
          "What a pilot would measure",
          "Recommendation relevance, research time, adviser preparation time and progress to interviews."
        ]
      ],
      "adviserChoice": "Agree what progress to review and which information Alex is comfortable sharing.",
      "adviserNote": "Programme reporting and candidate-controlled sharing are part of the proposed commercial service."
    }
  ];
  let selected = 2;
  let perspective = "candidate";
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
  const viewButtons = Array.from(document.querySelectorAll("[data-view]"));
  const byId = id => document.getElementById(id);
  const text = (id, value) => { byId(id).textContent = value; };
  function rowList(rows) {
    const list = document.createElement("ul");
    list.className = "demo-list";
    for (const [heading, detail] of rows) {
      const li = document.createElement("li");
      const strong = document.createElement("strong");
      const span = document.createElement("span");
      strong.textContent = heading;
      span.textContent = detail;
      li.append(strong, span);
      list.append(li);
    }
    return list;
  }
  function render() {
    const stage = stages[selected];
    const adviser = perspective === "adviser";
    tabs.forEach((tab, index) => {
      const active = index === selected;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    viewButtons.forEach(button => button.setAttribute("aria-pressed", String(button.dataset.view === perspective)));
    byId("journey-panel").setAttribute("aria-labelledby", `step-${selected}`);
    text("stage-kicker", `${String(selected + 1).padStart(2, "0")} / ${stage.name}`);
    text("stage-title", stage.title);
    text("stage-description", stage.description);
    text("stage-input", stage.input);
    text("stage-work", stage.work);
    text("stage-result", stage.result);
    document.querySelector(".example-heading .eyebrow").textContent = adviser ? "Proposed adviser experience" : "Alex’s workspace";
    text("example-title", adviser ? stage.adviserTitle : stage.exampleTitle);
    const body = byId("example-body");
    body.replaceChildren();
    if (adviser) {
      body.append(rowList(stage.adviserRows));
    } else {
      if (stage.label) {
        const label = document.createElement("p");
        label.className = "evidence-label";
        label.textContent = stage.label;
        body.append(label);
      }
      body.append(rowList(stage.rows));
    }
    text("choice-label", adviser ? "Candidate permission comes first" : "Alex stays in control");
    text("choice-copy", adviser ? stage.adviserChoice : stage.choice);
    text("perspective-note", adviser ? stage.adviserNote : "Fictional example. Adviser accounts and sharing controls are still to be built.");
    const next = byId("next-stage");
    next.firstChild.textContent = selected === 5 ? "Back to Direction " : `Next: ${stages[selected + 1].name} `;
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => { selected = index; render(); });
    tab.addEventListener("keydown", event => {
      let next;
      if (event.key === "ArrowRight") next = (selected + 1) % stages.length;
      if (event.key === "ArrowLeft") next = (selected + stages.length - 1) % stages.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = stages.length - 1;
      if (next === undefined) return;
      event.preventDefault();
      selected = next;
      render();
      tabs[next].focus();
    });
  });
  viewButtons.forEach(button => button.addEventListener("click", () => {
    perspective = button.dataset.view;
    render();
  }));
  byId("next-stage").addEventListener("click", () => {
    selected = (selected + 1) % stages.length;
    render();
    tabs[selected].focus({preventScroll: true});
  });
  render();
})();
