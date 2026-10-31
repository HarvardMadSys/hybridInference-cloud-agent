// How the Context view's parts open what they name: a request's or an event's
// step in the trajectory, or its turn when the step has not been read into the
// page.

export type OpenPoint = (target: { jobId: string; turnNo: number; eventId: number | null }) => void;
export type OpenTurn = (jobId: string, turnNo: number) => void;
