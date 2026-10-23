# Native CYC layouts verified for BatteryInsight 3.1

The file header has a 332-byte prefix containing a column count and a version-dependent reserved WORD column table. The count describes float values in each record, not the capacity of the reserved table.

| Header magic | Reserved column slots | Actual record start | Record size |
|---|---:|---:|---:|
| 20180615 / 4100 | 36 | 404 | 4 × declared column count |
| 20220405 / 4101 | 42 | 416 | 4 × declared column count |

In the supplied 4100 files there are 30 floats per record; in the supplied 4101 file there are 37. The 4101 file contains exactly 376,758 records. All records are little-endian float32. Required column IDs are DataSeq=22, StepTime=6, Voltage=1, Capacity=3. Each float32 is promoted exactly to float64 for storage. Physical voltage/capacity units remain unverified.

The prior 4100 reader started at 392 bytes and used offsets shifted by 12 bytes, with 12 bytes left at the end. Its selected DataSeq/time/voltage/capacity values were correct for the verified 4100 inputs. Those assumptions could not parse the 4101 header and 148-byte records. The current reader uses the actual record start and declared column IDs for both versions; it does not discard trailing fragments, round values, or infer a layout from plausible voltages.

Evidence used: `pne_data_structure.xlsx`, sheets `cell_fixed` (CycItemCode and PS_RECORD_FILE_HEADER) and `cell_ver_depending` (column-table capacities for 0x1004/0x1005), the previously supplied 4100 recordings, and the user-supplied 4101 recording dated 2026-09-09. Regression QA compares every selected value with a separate NumPy reader using literal byte offsets. Actual recordings are excluded from source distribution and Git.

Only the already verified 4100 CTS 896/172 layout is used for optional cycle/step/total-time labels. Missing or unsupported CTS leaves those labels null and preserves every CYC row. CYC conversion never requires CTS or Restore CSV.

Unknown versions, duplicate/missing required columns, nonzero unused header slots, and incomplete float records fail explicitly. Input files are read-only. Conversion results retain version, column IDs, actual offsets, source hashes, and warnings.
