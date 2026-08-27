"""The 17 national RTT status codes.

Official descriptions quoted from NHS England, "Referral to treatment
consultant-led waiting times: How to Measure", section 1.3.1.

clock_effect is the national CLASSIFICATION. It is not the same thing as what
the clock actually does - code 33 is classified STOP, but only nullifies where
the appointment was demonstrably communicated. Reference data classifies; it
does not decide.
"""

# code, official description, clock_effect, plain-English wording for the app
RTT_STATUS = [
    ("10", "First activity in a referral to treatment period",
     "START", "Referred - clock starts"),
    ("11", "Active monitoring end - first activity at the start of a new referral to treatment period following active monitoring",
     "START", "Decision to treat after active monitoring - new clock starts"),
    ("12", "Consultant referral - the first activity at the start of a new referral to treatment period following a decision to refer directly to the consultant for a separate condition",
     "START", "Referred for a new condition - clock starts"),
    ("20", "Subsequent activity during a referral to treatment period - further activities anticipated",
     "CONTINUE", "Activity on the pathway - clock continues"),
    ("21", "Transfer to another health care provider - subsequent activity during a referral to treatment period anticipated by another health care provider",
     "CONTINUE", "Transferred to another provider - clock continues"),
    ("30", "First treatment - the start of the first treatment that is intended to manage a patient's disease, condition or injury in a referral to treatment period",
     "STOP", "Treatment started - clock stops"),
    ("31", "Start of active monitoring initiated by the patient",
     "STOP", "Active monitoring at patient's request - clock stops"),
    ("32", "Start of active monitoring initiated by the care professional",
     "STOP", "Active monitoring started by clinician - clock stops"),
    ("33", "Failure to attend - the patient failed to attend the first care activity after the referral",
     "STOP", "Did not attend first appointment"),
    ("34", "Decision not to treat - decision not to treat made or no further contact required",
     "STOP", "Decision not to treat - clock stops"),
    ("35", "Patient declined offered treatment",
     "STOP", "Patient declined treatment - clock stops"),
    ("36", "Patient died before treatment",
     "STOP", "Patient died before treatment - clock stops"),
    ("90", "After treatment - first treatment occurred previously",
     "NOT_RTT", "Activity after treatment"),
    ("91", "Active monitoring - care activity during period of active monitoring",
     "NOT_RTT", "Activity during active monitoring"),
    ("92", "Not yet referred - not yet referred for treatment, undergoing diagnostic tests by GP before referral",
     "NOT_RTT", "Not yet referred - diagnostics with the GP"),
    ("98", "Not applicable - activity not applicable to referral to treatment periods",
     "NOT_RTT", "Activity outside the RTT pathway"),
    ("99", "Not yet known",
     "NOT_RTT", "Status not recorded"),
]

HEADER = ["rtt_status_code", "official_description", "clock_effect",
          "patient_facing_description"]
