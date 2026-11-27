import Lean.Data.Json
import LabRehearsalLean

/-!
# CLI Differential Runner for `LabRehearsalLean.checkRehearsal`

Reads a JSON batch `{"cases": [...]}` from the file path passed as the first CLI
argument, executes `LabRehearsalLean.checkRehearsal` on every case in exact
tenths-of-a-microliter (`0.1 uL`) integer arithmetic, and prints
`{"results": [...]}` to stdout.
-/

open Lean
open LabRehearsalLean

private def getBoolOr (j : Json) (field : String) (defaultVal : Bool) : Except String Bool := do
  match j.getObjVal? field with
  | .ok v => fromJson? v
  | .error _ => pure defaultVal

private def parseWell (j : Json) : Except String WellState := do
  let wellId ← j.getObjValAs? Nat "wellId"
  let vol ← j.getObjValAs? Nat "vol"
  let cap ← j.getObjValAs? Nat "cap"
  let deadVol ← j.getObjValAs? Nat "deadVol"
  let soluteAmt ← j.getObjValAs? Nat "soluteAmt"
  let hasBuffer ← getBoolOr j "hasBuffer" false
  let hasSample ← getBoolOr j "hasSample" false
  let frozen ← getBoolOr j "frozen" false
  let sealed ← getBoolOr j "sealed" false
  let isWaste ← getBoolOr j "isWaste" false
  let isReagentStock ← getBoolOr j "isReagentStock" false
  pure {
    wellId := wellId
    vol := vol
    cap := cap
    deadVol := deadVol
    soluteAmt := soluteAmt
    hasBuffer := hasBuffer
    hasSample := hasSample
    frozen := frozen
    sealed := sealed
    isWaste := isWaste
    isReagentStock := isReagentStock
  }

private def parseStep (j : Json) : Except String RehearsalStep := do
  let kind ← j.getObjValAs? String "kind"
  match kind with
  | "pickTip" => pure .pickTip
  | "dropTip" => pure .dropTip
  | "aspirate" =>
    let srcId ← j.getObjValAs? Nat "srcId"
    let vol ← j.getObjValAs? Nat "vol"
    pure (.aspirate srcId vol)
  | "dispense" =>
    let dstId ← j.getObjValAs? Nat "dstId"
    let vol ← j.getObjValAs? Nat "vol"
    let airDispense ← getBoolOr j "airDispense" false
    pure (.dispense dstId vol airDispense)
  | "transfer" =>
    let srcId ← j.getObjValAs? Nat "srcId"
    let dstId ← j.getObjValAs? Nat "dstId"
    let vol ← j.getObjValAs? Nat "vol"
    pure (.transfer srcId dstId vol)
  | "mix" =>
    let wellId ← j.getObjValAs? Nat "wellId"
    let vol ← j.getObjValAs? Nat "vol"
    let reps ← j.getObjValAs? Nat "reps"
    pure (.mix wellId vol reps)
  | "incubate" =>
    let wellId ← j.getObjValAs? Nat "wellId"
    let tempC ← j.getObjValAs? Nat "tempC"
    let durSec ← j.getObjValAs? Nat "durSec"
    pure (.incubate wellId tempC durSec)
  | "sealWell" =>
    let wellId ← j.getObjValAs? Nat "wellId"
    pure (.sealWell wellId)
  | "thawWell" =>
    let wellId ← j.getObjValAs? Nat "wellId"
    pure (.thawWell wellId)
  | other => .error s!"unknown RehearsalStep kind: {other}"

private def evalCase (j : Json) : Except String Json := do
  let caseId ← j.getObjValAs? String "id"
  let protoHash ← j.getObjValAs? Nat "protoHash"
  let pipMin ← j.getObjValAs? Nat "pipMin"
  let pipMax ← j.getObjValAs? Nat "pipMax"
  let failedArr ← j.getObjValAs? (Array Nat) "failedHashes"
  let wellsJson ← (j.getObjVal? "wells") >>= Json.getArr?
  let stepsJson ← (j.getObjVal? "steps") >>= Json.getArr?
  let wells ← (wellsJson.mapM parseWell).map Array.toList
  let steps ← (stepsJson.mapM parseStep).map Array.toList
  let deck : DeckState := {
    wells := wells
    tip := .noTip
    pipMin := pipMin
    pipMax := pipMax
    failedHashes := failedArr.toList
  }
  match checkRehearsal deck protoHash steps with
  | .ok sEnd =>
    pure (Json.mkObj [
      ("id", Json.str caseId),
      ("ok", Json.bool true),
      ("rule", Json.null),
      ("detail", Json.null),
      ("totalWellsVolTenths", toJson (totalWellsVol sEnd.wells)),
      ("totalWellsSoluteTenths", toJson (totalWellsSolute sEnd.wells))
    ])
  | .error v =>
    pure (Json.mkObj [
      ("id", Json.str caseId),
      ("ok", Json.bool false),
      ("rule", Json.str v.rule),
      ("detail", Json.str v.detail)
    ])

def main (args : List String) : IO UInt32 := do
  match args with
  | [inputPath] =>
    let raw ← IO.FS.readFile inputPath
    let doc ← match Json.parse raw with
      | .ok d => pure d
      | .error err =>
        IO.eprintln s!"JSON parse error: {err}"
        return 2
    let casesArr ← match (doc.getObjVal? "cases") >>= Json.getArr? with
      | .ok arr => pure arr
      | .error err =>
        IO.eprintln s!"Missing cases array: {err}"
        return 2
    match casesArr.mapM evalCase with
    | .ok results =>
      let outJson := Json.mkObj [("results", Json.arr results)]
      IO.println outJson.compress
      return 0
    | .error err =>
      IO.eprintln s!"Case evaluation error: {err}"
      return 2
  | _ =>
    IO.eprintln "Usage: lab-rehearsal-check <cases.json>"
    return 1
