from qiskit_ibm_runtime import QiskitRuntimeService as S
ids=['666d5d67-7d3c-44d8-9200-f8cb2c7b9fc4','96d0b902-29e2-4414-92b2-8751d31e930f','328a15b7-6d39-4e71-9519-1bcc0e05f94a','1529dca0-10c6-4e17-a9e9-84decc3c5ead','8d7cb0cc-bf9b-4bec-8a5e-6179af9667a2','39c1c5e7-a7c5-459d-baf8-4181d6fbf697','821da9ab-5928-44e2-ab37-4ba39961c2b6','c015165f-d871-49e7-bce9-6461be41afd8']
s=S()
for jid in ids:
    j=s.job(jid)
    print(jid, j.backend(), j.status(), j.creation_date())
    r=j.result()
    print('keys=', list(r.keys()) if hasattr(r,'keys') else None)
    print('---')

