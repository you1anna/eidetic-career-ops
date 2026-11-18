// The words the workspace shows, in one place, so every page uses the same terms.
// Approach statuses and kinds come from the server (state.intelligence.labels).
export const FIELD_NAMES = {
  preferred_channel: "Channel",
  preferred_ask: "Kind of ask",
  priority: "Your list",
  ir35: "IR35",
  evidence_urls: "Evidence",
  url: "Link",
  source_lifecycle: "Advert status",
  published_deadline: "Closing date",
  next_action: "Next step",
  last_touch: "Last touch",
  working_state: "Work state",
};
// A link's relation as seen from the open record: [when it is the link's from side, when it is the to side].
export const RELATION_NAMES = {
  hiring_for: ["Hiring organisation", "Vacancy"],
  consultant_at: ["Consultant at", "Consultant"],
  works_at: ["Works at", "Works here"],
  introducer: ["Introduces", "Introduced by"],
  buyer: ["Buyer", "Tender"],
  possible_match: ["Possible duplicate", "Possible duplicate"],
  related: ["Related", "Related"],
};
export const ACTOR_NAMES = {
  owner: "You",
  agent: "Assistant",
  source: "Source",
  research: "Research",
  review: "Review",
};
export const BAND_NAMES = { strong: "Strong", possible: "Possible", low: "Lower" };
// Lanes are derived with the relevance band (config/relevance.yml `lanes`); contract and client work comes first.
export const LANE_ORDER = ["contract", "pipeline", "permanent", "unstated", "event"];
export const LANE_NAMES = {
  contract: "Contract and client work",
  pipeline: "Your pipeline",
  permanent: "Permanent roles",
  unstated: "Terms not stated",
  event: "Events",
};
export const ENGAGEMENT_NAMES = {
  contract: "Contract",
  interim: "Interim",
  ftc: "Fixed term",
  fractional: "Fractional",
  procurement: "Tender",
  permanent: "Permanent",
};
export const JOB_STATUS_NAMES = {
  candidate: "Discovered", pursue: "Shortlisted", skip: "Skipped", applied: "Applied", responded: "Responded",
  interview: "Interview", offer: "Offer", rejected: "Rejected", closed: "Closed",
};
export const JOB_ACTION_NAMES = {
  candidate: "Return to discovery", pursue: "Shortlist", skip: "Skip", applied: "I applied",
  responded: "They responded", interview: "Interview arranged", offer: "Offer received",
  rejected: "Record rejection", closed: "Close application",
};
// How an approach reaches the person: the relationship it relies on.
export const ROUTE_NAMES = {
  close_network: "Close network", known_contact: "Known contacts", agency: "Agency desks", recruiter: "Recruiters",
  direct: "Direct approach", opportunity: "Work opportunities",
};
// Recorded outcomes, as the history shows them.
export const OUTCOME_NAMES = {
  sent: "Sent", followed_up: "Followed up", replied: "Replied", introduction: "Introduction offered",
  referral: "Referral given", submitted: "CV submitted", conversation: "Spoke", proposal: "Proposal",
  won: "Engagement won", no_fit: "No fit", no_budget: "No current budget", permanent_only: "Permanent only",
  no_response: "No response", handed_over: "Handed over to Work",
};
// Outreach page sections, in the order they appear.
// The owner's list (Release 2): his own words for each person's place, channel, ask, tie and role.
export const PRIORITY_NAMES = { first: "First priority", next: "Next" };
export const CHANNEL_NAMES = {
  call: "Call", whatsapp: "WhatsApp", text: "Text", email: "Email", linkedin: "LinkedIn", in_person: "In person",
  meeting: "Meeting", event: "Event", form: "Form", other: "Other",
};
export const ASK_NAMES = { catch_up: "Catch-up", introduction: "Introduction", availability: "Availability" };
export const RELATIONSHIP_NAMES = {
  warm: "Know well", active: "In touch", dormant: "Know from before", cold: "Don't know them",
  none: "Not stated", do_not_contact: "Not contacting",
};
export const TIER_NAMES = {
  close: "Know well", in_touch: "In touch", known: "Know from before", connected: "Connected", cold: "Not known",
  blocked: "Not contacting",
};
export const KIND_NAMES = {
  former_colleague: "Former colleague", former_manager: "Hired or managed you", recruiter: "Recruiter",
  in_house_talent: "In-house recruiter", hiring_manager: "Hiring manager", peer: "Peer", friend: "Friend",
  founder: "Founder", introducer: "Introducer", other: "Other",
};
// The value words for person fields wherever a record's details are shown.
export const VALUE_NAMES = {
  priority: PRIORITY_NAMES, preferred_channel: CHANNEL_NAMES, preferred_ask: ASK_NAMES,
  relationship: RELATIONSHIP_NAMES,
};
