import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BridgeDeps } from "../lib/meet/bridge";
import { MeetCopilotPanel } from "./MeetCopilotPanel";

function failingDeps(code: string): () => Promise<BridgeDeps> {
  return async () => ({
    createClient: () => ({
      sessionStatus: { get: () => ({ connectionState: 0 }), subscribe: () => () => undefined, unsubscribe: () => true },
      meetStreamTracks: { get: () => [], subscribe: () => () => undefined, unsubscribe: () => true },
      joinMeeting: async (p) => {
        await p?.connectActiveConference("v=0");
      },
      leaveMeeting: async () => undefined,
    }),
    createAudioSink: async () => ({ addTrack: () => undefined, close: async () => undefined }),
    openSocket: vi.fn(),
    connect: async () => {
      throw Object.assign(new Error("x"), { code });
    },
    reportEvent: async () => undefined,
    newId: () => "event-id-1",
  });
}

describe("MeetCopilotPanel", () => {
  it("only connects once the call is in progress", () => {
    render(<MeetCopilotPanel callId="c1" callLive={false} meetingUrl="https://meet.google.com/abc-defg-hij" />);
    expect(screen.getByRole("button", { name: "Connect copilot to meeting" })).toBeDisabled();
    expect(screen.getByText(/Start the call first/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open meeting" })).toHaveAttribute("href", "https://meet.google.com/abc-defg-hij");
  });

  it("explains a Developer Preview eligibility failure", async () => {
    render(
      <MeetCopilotPanel
        callId="c1"
        callLive
        meetingUrl={null}
        depsFactory={failingDeps("MEET_MEDIA_API_NOT_ELIGIBLE")}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Connect copilot to meeting" }));
    expect(await screen.findByText(/Developer Preview/)).toBeInTheDocument();
    expect(screen.getByText("(MEET_MEDIA_API_NOT_ELIGIBLE)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reconnect" })).toBeEnabled();
  });

  it("explains the consumer-meeting consent requirement", async () => {
    render(<MeetCopilotPanel callId="c1" callLive meetingUrl={null} depsFactory={failingDeps("MEET_CONSENT_REQUIRED")} />);
    await userEvent.click(screen.getByRole("button", { name: "Connect copilot to meeting" }));
    expect(await screen.findByText(/For a Gmail meeting, the person who started it must be present/)).toBeInTheDocument();
  });
});
