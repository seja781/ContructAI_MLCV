# Step 3: data cleaning

**68,978 rows in → 63,687 rows out** (5,291 removed, 7.7%).

## Rows removed
| Rule | Rows |
|---|---|
| area outside 250-10000 sqft | 89 |
| price outside 5-2000 lakh | 10 |
| INR/sqft outside 2,000-40,000 | 30 |
| bedrooms outside 1-6 | 14 |
| bathrooms more than bedrooms + 2 | 9 |
| floors impossible (building > 60 floors, or flat above top floor) | 7 |
| exact duplicates after cleaning | 5,132 |
| **Total** | **5,291** |

## Values changed (rows kept)
| Change | Count |
|---|---|
| locality names standardised (rows) | 3573 |
| localities before -> after | 1346 -> 1312 |
| coordinates outside Pune emptied (rows kept) | 308 |

Localities after cleaning: 1,310 (377 with 10+ listings).

## Locality names merged (top 40)
| From | To | Rows |
|---|---|---|
| Hinjawadi | Hinjewadi | 1646 |
| Charholi BK | Charholi Budruk | 252 |
| Phase 3 Hinjewadi Rajiv Gandhi Infotech Park | Hinjewadi Phase 3 | 178 |
| Manjari | Manjri | 178 |
| Ambegaon BK | Ambegaon Budruk | 167 |
| Phase 1 Hinjewadi Rajiv Gandhi Infotech Park | Hinjewadi Phase 1 | 166 |
| Kondhwa BK | Kondhwa Budruk | 144 |
| Manjari Khurd | Manjri Khurd | 97 |
| Phase 2 Hinjewadi Rajiv Gandhi Infotech Park | Hinjewadi Phase 2 | 90 |
| Hinjewadi Rajiv Gandhi Infotech Park | Hinjewadi | 89 |
| NIBM | NIBM Road | 87 |
| Manjari BK | Manjri Budruk | 75 |
| Karve Nagar | Karvenagar | 67 |
| Vadgaon BK | Vadgaon Budruk | 63 |
| Wadgaon BK | Vadgaon Budruk | 51 |
| Lulla Nagar | Lullanagar | 19 |
| Law collage Road | Law College Road | 18 |
| Baner Highway Side Rd | Baner Highway Side Road | 17 |
| Dhanakwadi | Dhankawadi | 17 |
| Bhawani Peth | Bhavani Peth | 16 |
| Shivaji Nagar | Shivajinagar | 15 |
| Manjri BK | Manjri Budruk | 11 |
| Ambegaon KH | Ambegaon Khurd | 10 |
| bhekarai nagar | Bhekrai Nagar | 10 |
| Vadgaon Sheri | Wadgaon Sheri | 10 |
| Hinjawadi Village | Hinjewadi | 9 |
| Dattanagar Ambegaon BK | Dattanagar Ambegaon Budruk | 9 |
| Manjari Budruk | Manjri Budruk | 9 |
| Bhoslenagar | Bhosle Nagar | 8 |
| Ambegaon Bk | Ambegaon Budruk | 7 |
| Ambe Gaon | Ambegaon | 7 |
| Phase 2 Hinjawadi | Hinjewadi Phase 2 | 5 |
| Varve Bk | Varve Budruk | 4 |
| Shankar Sheth Rd | Shankar Sheth Road | 4 |
| Hinjawadi Phase 3 | Hinjewadi Phase 3 | 4 |
| Hingne BK | Hingne Budruk | 2 |
| DhanoriLohegaon Road | Dhanori Lohegaon Road | 2 |
| Tukaram Nagar Ambegaon BK | Tukaram Nagar Ambegaon Budruk | 1 |
| Ghule Nagar Manjari BK | Ghule Nagar Manjari Budruk | 1 |
| Katraj Ambegaon BK Road | Katraj Ambegaon Budruk Road | 1 |