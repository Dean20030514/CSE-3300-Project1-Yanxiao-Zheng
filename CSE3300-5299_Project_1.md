# Project 1: Socket Programming

**Due:** Saturday, October 25, 2025 (Midnight)

Please mark in your submission whether you are in **CSE5299** or **CSE3300** in your submitted file.

- **Scoring (CSE3300):** 100 points with up to 20 extra points (capped at 100 in final grade calculation).  
- **Scoring (CSE5299):** 120 points (treated as 120 in final grade calculation).

---

## 1. Purpose of Assignment

In this assignment, you will practice **TCP socket programming**.

- **Part 1:** Write a client and a **single-threaded** server.  
- **Part 2:** Extend the server to a **multi-threaded** server.

You may use any programming language, but **Python is strongly encouraged** (sample code in class and the provided sample multi-threaded server are in Python).

---

## 2. Basic Assignment

Implement a client that queries for English words stored in a word list on a server using **TCP** (for reliability).

- The client sends a **wildcard query** (via keyboard input) to the server.  
- The only wildcard you need to support is the **question mark `?`**, which may appear one or multiple times.  
- Example query: `a?t` means: three-letter words starting with `a`, ending with `t`, and any single letter in the middle.

**Server behavior:**  
The server maintains a text file `wordlist.txt` (provided) containing a list of English words. It waits for client queries; upon receiving a query, it searches the list and returns **all matching words** to the client. The **client displays** the returned words and **then terminates**. The server needs to serve **one client at a time**. The client only needs to send **one query**. For testing, it is OK to limit cases to **up to 3 `?`** characters.

### Application Protocol

Design and describe your **application-level protocol**, including:

- **Message formats** (queries and responses) and the **actions** for server and client.  
- Consider **special cases** (e.g., no matches).  
- You may use action/status codes similar to HTTP, e.g., `200 OK`, `404 Not Found`.  
- The **same** application protocol must work for both the **basic** and **multi-threaded** versions.  
- **Requirements:**  
  - **Client request** must contain a **command**.  
  - **Server response** must contain a **status code** and the **number of matching words**.

### Objective

Responses must **exactly match the pattern** in both **length** and **letter placement**.

- **Example:** Query `a?t` → include all **3-letter** words that start with `a` and end with `t` (with any single middle character).

---

## 3. Multi-threaded Server

Now set aside copies of your basic server and client (submit **both** versions). Extend both programs:

1. Make the server **multi-threaded** so it can serve **multiple clients simultaneously**.  
2. Extend the client to allow **multiple queries** (entered via keyboard). The client terminates when you input `quit`.

A sample multi-threaded server `thread-server.py` (from Mark Lutz’s *Programming Python*) is provided. Read the `dispatcher` function, which delegates each client to a new thread running `handleClient`, enabling concurrent service.

**Testing concurrent service:**

1. Run the server in **one terminal**.  
2. Open **another terminal** to run **Client 1** (do not terminate it).  
3. Open **yet another terminal** to run **Client 2**.

---

## 4. What to Turn In

Submit to **HuskyCT**:

- **Program code** with **in-line documentation**. Submit **both**:  
  - Basic server and client.  
  - Multi-threaded server and client.

- A **separate, typed design document** (for the **multi-threaded** versions) describing:  
  - **Description:** Overall program design, “how it works,” and your **application protocol** (you may refer to protocols like **HTTP**, **DNS**, **SMTP** as examples).  
  - **Tradeoffs:** Design tradeoffs you considered and made.  
  - **Extensions:** Possible improvements/extensions and a brief plan to implement them.  
  - **Test cases:** Test cases demonstrating correctness, including **screenshots**. Also list any known non-working cases.

> **Format note:** If your design document does **not** follow the above format, **10 points** will be deducted automatically.

---

## 5. Grading Policy (Total: 100 points)

### Program Listing
- Works correctly (shown by test results): **50**  
- In-line documentation: **10**  
- Quality of design: **10**

### Design Document
- Description: **5**  
- Tradeoffs discussion: **5**  
- Extensions discussion: **5**

### Thoroughness of Test Cases
- With **screenshots**: **15**

> **Note:** A full **10 points** for *quality of design* will only be given to **well-designed, thorough** programs. A correctly working, documented, and tested program will **not necessarily** receive these 10 points.

---

## 6. For CSE5299 Students; Extra Credits for CSE3300 (20 points)

This section is **mandatory** for **CSE5299** students and gives up to **20 extra points** for **CSE3300** students. Mark your course in your submission; otherwise, it will be treated as a **CSE3300** submission.

**Continue** from the multi-threaded server & multi-query client:

1. **Server:** Support queries containing **any number of wildcards** using **partial match** and **print the total count** of words per response.  
   - Effectively interpret the query `a?t` as `*a?t*`, where `*` matches any string of length **≥ 0**.  
   - Example matching words include: `ant`, `anticipate`, `mantis`, `rant`, etc. (e.g., `ant` matches).

2. **Client:** Print the **entire response** and the **total number of words** contained in the server’s response **before** making another query.

**Required example client queries and expected counts:**

- `??????????` → **24071** matches  
- `?` → **69903** matches  
- `?(a)` → **414** matches  
- `-?-` → **13** matches

(Include these test cases; feel free to add more.)

### Grading (this part)
- Program with in-line documentation: **5**  
- Thoroughness of test cases (screenshots for both client and server): **5**  
- Server prints **correct counts** for **all** test cases: **5**  
- Client receives the **entire response** and prints **correct counts** for **all** test cases: **5**

---

## 7. A Few Words About Borrowing Code…

You may learn from and borrow publicly available sample socket code **with attribution**. Follow these guidelines:

- Do **not** borrow code written by a fellow student for the **same** project.  
- **Acknowledge sources** of borrowed code (book or URL). No need to reference code explicitly given to you.  
- In your **design document**, clearly identify which portions are borrowed vs. self-written, and explain modifications/extensions. Also **comment** in your source code to distinguish borrowed vs. original code.  
- Use only code that is **freely available** in the **public domain**.

---

*End of document.*
