# GAO-Bid-Protest-Dataset
Dataset of ~5700 GAO bid protest decisions 

Frequently Asked Questions (FAQ)

1. What is this dataset and what are the different files?

This dataset is a structured collection of U.S. Government Accountability Office (GAO) bid protest decisions from 2017-2025. The three datasets are identical in text. The CSV and Excel files are self-explanitory. The JSON has undergone initial pre-processing, chunking, and some metadata is classified. I have found that the json is too large for some commercial platforms. 

2. What types of documents are included?

All decisions between December 17, 2017 and December 22, 2025, and metadata such as B-number, date, and decision name

3. What is the source of the data?
   
All case law originates from publicly available GAO decisions, primarily published on official U.S. government websites.

4. Is this dataset official or endorsed by GAO?

No, this dataset is not a government product. It was not developed at at government expense or on government time, and was the personal project of its creator. All decisions within are government products. 
This dataset should not be treated as a substitute for official GAO publications. Users should always verify critical information against the original GAO source documents.

5. What is the intended use of this dataset?

This dataset is intended for use in Retrieval-Augmented Generation (RAG) systems, where GAO bid protest case law is indexed, embedded, and retrieved to provide factual grounding and source-based context for large language model (LLM) outputs.
This dataset is suitable for ingestion into Retrieval-Augmented Generation (RAG) pipelines, including use in vector databases for semantic retrieval, chunked document indexing, and grounding LLM responses in authoritative GAO bid protest case law.

It is not intended to provide legal advice.

6. Can this dataset be used to train AI or machine learning models?

Yes.The dataset is explicitly designed to be AI-ingestion friendly, and future updates will hopefully expand that suitability. Users are responsible for ensuring compliance with applicable laws, licenses, and ethical AI practices when training models.

7. Does this dataset contain personally identifiable information (PII)?

GAO decisions are publicly released and generally redacted. However, the dataset may still contain names of companies, agencies, or representatives as publicly published by GAO.
Users should not assume the dataset is fully anonymized and should apply appropriate safeguards if required.

8. How current is the dataset?

The decisions were collected on December 22, 2025, so that is the final date. Updates will be made if possible, but should not be expected. 

9. Are there known limitations or risks?

Yes. Potential limitations include:
A. Changes in GAO legal standards over time
B. An absence of full Court of Federal Claims, Court of Appeals for the Federal Circuit, FAR text, and statutory text 
C. Changes due to the Revolutionary FAR Overhaul 
D. Noise resulting from the scrape. 

AI systems should not assume uniformity or completeness across all entries.

10. Can I rely on this dataset for legal conclusions?

No. While this dataset is intended to provide helpful context data for AI systems, it is provided as is, without any warranty or guarantee that using it will reduce or eliminate confabulations/hallucinations. It does not constitute legal advice, and outcomes should not be relied upon without consulting other sources. 

11. What license governs this dataset?

Licensing details are provided in the repository’s LICENSE file.
While GAO decisions themselves are generally public domain, dataset structure, annotations, or enhancements may be subject to additional licensing terms.

12. How should this dataset be cited?

If you use this dataset, please cite the GitHub repository. Note that the data is derived from public GAO decisions. Include the dataset version or commit hash if possible. If you use it for any purpose, please let me know, I'd be thrilled to know what you're working on. 

13. How can issues or errors be reported?

Users are encouraged to open an issue in the GitHub repository

14. Who maintains this dataset?

Maintenance details, including contributors and maintainers, are listed in the GitHub repository documentation. This dataset is maintained by me, not GAO.

15. Next Steps:
1. Expand metadata extraction and classification
2. Remove noise from dataset
3. Condense file size 
