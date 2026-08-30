import { useState, useEffect, useRef } from "react";
import Vapi from "@vapi-ai/web";
import { startVoiceCall, completeVoiceCall } from "../api";

export type CallState = "idle" | "starting" | "connecting" | "active" | "ending" | "done" | "error";

export function useVapiCall(onComplete?: () => void) {
    const [callState, setCallState] = useState<CallState>("idle");
    const [transcript, setTranscript] = useState<string>("");
    const [outcome, setOutcome] = useState<any>(null);
    const [errorMsg, setErrorMsg] = useState<string | null>(null);

    const vapiRef = useRef<any>(null);
    const callLogRef = useRef<number | null>(null);
    const startTimeRef = useRef<number>(0);
    const firstSpeechRef = useRef<number | null>(null);
    const transcriptRef = useRef<string>("");

    const updateTranscript = (text: string) => {
        transcriptRef.current = text;
        setTranscript(text);
    };

    const startCall = async (caseId: string, clientName: string, amount: number | undefined, condition: string, phone: string, caseKey: string) => {
        try {
            setCallState("starting");
            updateTranscript("");
            setOutcome(null);
            setErrorMsg(null);
            firstSpeechRef.current = null;
            startTimeRef.current = Date.now();

            const res = await startVoiceCall({ case_id: caseId, client_name: clientName, amount, condition, phone, case_key: caseKey });
            callLogRef.current = res.call.id as number;

            // There is no simulated path. Either a real browser WebRTC session
            // opens against Vapi, or the call fails loudly. Nothing is ever
            // written as an outcome unless a real conversation produced it.
            if (!res.web || !res.web.public_key) {
                throw new Error("Voice calling is not configured: VAPI_PUBLIC_KEY is missing on the server.");
            }

            const vapi = new Vapi(res.web.public_key);
            vapiRef.current = vapi;

            vapi.on("call-start", () => {
                setCallState("active");
            });

            vapi.on("speech-start", () => {
                if (firstSpeechRef.current === null) {
                    firstSpeechRef.current = (Date.now() - startTimeRef.current) / 1000;
                }
            });

            vapi.on("message", (msg: any) => {
                if (msg.type === "transcript" && msg.transcriptType === "final") {
                    updateTranscript(transcriptRef.current + "\n" + (msg.role === "user" ? "Client: " : "Agent: ") + msg.transcript);
                }
            });

            vapi.on("call-end", (async (msg: any) => {
                setCallState("ending");
                try {
                    const completeRes = await completeVoiceCall({
                        call_id: callLogRef.current!,
                        transcript: transcriptRef.current,
                        speech_detected: firstSpeechRef.current !== null,
                        seconds_to_first_speech: firstSpeechRef.current ?? undefined,
                        provider_call_id: msg?.call?.id || "",
                        ended_reason: msg?.reason || "customer-ended"
                    });
                    setOutcome(completeRes.classification);
                } catch (e: any) {
                    setErrorMsg(e.message);
                }
                setCallState("done");
                vapiRef.current = null;
                onComplete?.();
            }) as any);

            vapi.on("error", (e: any) => {
                setCallState("error");
                setErrorMsg(e.message || "Vapi error");
                vapiRef.current = null;
            });

            setCallState("connecting");
            if (res.web.assistantId) {
                await vapi.start(res.web.assistantId);
            } else if (res.web.assistant) {
                await vapi.start(res.web.assistant);
            } else {
                throw new Error("No assistant defined");
            }
        } catch (err: any) {
            setCallState("error");
            setErrorMsg(err.message);
        }
    };

    const endCall = () => {
        if (vapiRef.current) {
            vapiRef.current.stop();
        }
    };

    useEffect(() => {
        return () => {
            if (vapiRef.current) {
                vapiRef.current.stop();
            }
        };
    }, []);

    return { callState, transcript, outcome, errorMsg, startCall, endCall, reset: () => setCallState("idle") };
}
