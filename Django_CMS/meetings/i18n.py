"""Strings the meeting pages' scripts show, translated on the server.

Kept out of the templates' markup because a translation containing a quote
would break a hand-written JSON literal; ``json_script`` escapes these safely.
"""
from django.utils.translation import gettext as _


def staff():
    return {
      "you": _("You"),
      "assistant": _("Assistant"),
      "roles": {"navigator": _("Care team"), "caregiver": _("Caregiver"), "client": _("Client"), "other": _("Guest"), "guest": _("Guest"), "interviewer": _("AI interviewer"), "assistant": _("AI assistant"), "scribe": _("Records everyone's audio"), "agent": _("AI agent")},
      "agentStates": {"initializing": _("Joining…"), "listening": _("Listening"), "thinking": _("Thinking…"), "speaking": _("Speaking"), "paused": _("Paused")},
      "connecting": _("Connecting…"),
      "reconnecting": _("Connection lost — reconnecting…"),
      "permissionDenied": _("The browser was not allowed to use your microphone. Click the camera or lock icon in the address bar, allow the microphone, then reload."),
      "noDevice": _("No microphone was found. Plug one in, or choose another below."),
      "deviceBusy": _("Your microphone is being used by another app. Close it and try again."),
      "deviceGeneric": _("Your microphone could not be started."),
      "admit": _("Admit"), "deny": _("Turn away"),
      "knocking": _("is waiting to join"),
      "nobodyWaiting": _("Nobody is waiting. When someone opens their link, they appear here for you to let in."),
      "copied": _("Link copied."),
      "rotated": _("A new link was made. The old one no longer works."),
      "endConfirm": _("End the meeting for everyone?"),
      "ivNoProtocols": _("This client has no protocols with questions yet."),
      "ivNoOne": _("Nobody to interview yet — they appear here once they have joined."),
      "ivAsking": _("Interviewing"),
      "ivStarting": _("The interviewer is joining…"),
      "ivPaused": _("Paused"),
      "ivFinished": _("Interview finished."),
      "ivFailed": _("The interviewer stopped"),
      "ivOf": _("of"),
      "ivAnswered": _("answered"),
      "ivResume": _("Resume"), "ivPause": _("Pause"),
      "ivCarried": _("From an earlier meeting"),
      "ivNewAnswer": _("New answer recorded"),
      "askStart": _("Ask the assistant"), "askHold": _("Hold to talk"), "askListening": _("Listening — release to send"), "askJoining": _("Assistant joining…"),
      "recOn": _("Recording"), "recStarting": _("Starting recording…"), "recLost": _("Not recording"),
      "duplicate": _("You joined this meeting from another window or device, so this one was disconnected."),
      "removed": _("The meeting has ended."),
      "saved": _("Saved."),
      "sendFail": _("The link could not be sent."),
      "leftTitle": _("You left the meeting"),
      "endedTitle": _("The meeting has ended"),
      "selectMic": _("Microphone"), "selectCam": _("Camera"), "selectSpeaker": _("Speaker"), "testSpeaker": _("Play a test sound"),
      "defaultDevice": _("System default"),
      "leftText": _("The meeting is still open for the others. You can rejoin from here or from the meeting's panel."),
      "notConfigured": _("Online meetings are not connected to a meeting server yet. An administrator can finish the setup in Settings → Online meetings.")
    }


def client():
    return {
        "you": _("You"),
        "assistant": _("Assistant"),
        "roles": {"navigator": _("Care team"), "caregiver": _("Caregiver"), "client": _("Client"),
                  "other": _("Guest"), "guest": _("Guest"), "interviewer": _("AI interviewer"),
                  "assistant": _("AI assistant"), "agent": _("AI agent")},
        "agentStates": {"initializing": _("Joining…"), "listening": _("Listening"),
                        "thinking": _("Thinking…"), "speaking": _("Speaking"), "paused": _("Paused")},
        "permissionDenied": _("Your browser did not allow the microphone. Tap the lock or "
                              "camera icon next to the web address, allow the microphone, then "
                              "reload this page."),
        "noDevice": _("No microphone was found. If you are on a computer, plug in a headset "
                      "or choose another microphone below."),
        "deviceBusy": _("Your microphone is being used by another app. Close it and try again."),
        "deviceGeneric": _("Your microphone could not be started."),
        "camOff": _("Your camera is off"),
        "reconnecting": _("Your connection dropped — reconnecting…"),
        "waitingTitle": _("Waiting for %(host)s to let you in"),
        "waitingText": _("They have been told you are here. Keep this page open — you will join automatically."),
        "noHostTitle": _("%(host)s has not started the meeting yet"),
        "noHostText": _("Keep this page open. You will be let in once they join."),
        "deniedTitle": _("You were not let in"),
        "deniedText": _("If you think this is a mistake, ask to join again or contact your care team."),
        "askAgain": _("Ask to join again"),
        "joining": _("Joining…"),
        "join": _("Join the meeting"),
        "duplicate": _("You joined from another device or window, so this one was disconnected."),
        "unavailable": _("This meeting is not available any more."),
        "defaultDevice": _("System default"),
        "testSpeaker": _("Play a test sound"),
    }
