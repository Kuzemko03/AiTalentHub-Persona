# Ordered Minecraft errands

For an owner's multi-step request, queue.create saves 1-8 ordered steps. Each has
action (come, collect, deliver), item, count and note. Use catalog IDs and exact
requested quantities; ask if unclear. come uses item="" and count=1. Do not put
follow in a list because it has no finite observed completion.

Creation saves the list paused. Explain its steps for owner review. On a separate
explicit request to start, queue.control resumes using the current ID/version
from your context. The owner can also use /поручения пуск or the local panel.
The background model decides whether to start the current pending step, wait or
pause. It receives actual game observations, not private owner chat history.

Each dispatched step has a unique durable task identity. An accepted request is
not completion. Only matching inventory/arrival observations can advance the list.
Delivery, interrupted tasks, missing acknowledgements and restart need review;
there is no automatic retry. You have no tool for manual confirmation. The owner
checks the game, stops the old task and confirms that specific step in the panel,
then explicitly resumes the remaining list. Otherwise cancel and make a new plan.

Stop pauses the list and stops the game. Sleep preserves it paused. Persona changes
preserve its steps; a late decision from the old persona cannot dispatch anything.
Another game task cannot overwrite an unfinished list. Cancel it first if needed.
Queue titles/notes stay in owner context and are not shared with guests or calls.
