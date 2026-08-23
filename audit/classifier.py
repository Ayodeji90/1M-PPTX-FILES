"""
Graphics-Heavy Image Classifier

Uses OpenCV heuristics to score images 0-100 for "graphics-heavy" content:
- Charts (bar, pie, line, scatter)
- Graphs (data visualizations)
- Maps (geographic visualizations)
- Infographics (data-rich layouts)
- Diagrams (flowcharts, process diagrams)

Rejects:
- Text-heavy slides (bullet points, paragraphs)
- Photos (headshots, scenery)
- Decorative images (logos, clip art)
- Table/spreadsheet dumps
- Title/closing slides
"""
import cv2
import numpy as np
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ImageFeatures:
    """Features extracted from a single image."""
    filename: str
    width: int = 0
    height: int = 0
    
    # Text detection
    text_density: float = 0.0
    
    # Line/axis detection
    line_count: int = 0
    perpendicular_pairs: int = 0
    
    # Color analysis
    color_entropy: float = 0.0
    distinct_colors: int = 0
    
    # Edge analysis
    edge_density: float = 0.0
    long_edge_ratio: float = 0.0
    
    # Blob analysis
    medium_blobs: int = 0
    large_blobs: int = 0
    
    # Symmetry
    symmetry: float = 0.0
    
    # Background
    white_background: float = 0.0
    
    # Hue
    hue_concentration: float = 0.0
    hue_diversity: int = 0
    
    # Composite score
    score: int = 0
    
    # Classification
    classification: str = "pending"  # pass, borderline, reject
    rejection_reason: str = ""


def extract_features(image_path: str) -> ImageFeatures:
    """Extract all features from a single image."""
    img = cv2.imread(image_path)
    if img is None:
        return ImageFeatures(filename=Path(image_path).name, score=0, classification="reject", rejection_reason="cannot_read")
    
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    
    features = ImageFeatures(filename=Path(image_path).name, width=w, height=h)
    
    # === 1. TEXT DENSITY ===
    edges = cv2.Canny(gray, 50, 150)
    kernel_text = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3))
    text_mask = cv2.dilate(edges, kernel_text, iterations=2)
    text_contours, _ = cv2.findContours(text_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    text_area = 0
    for cnt in text_contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        aspect = cw / max(ch, 1)
        if aspect > 3 and ch < h * 0.1:
            text_area += cw * ch
    
    features.text_density = round(text_area / (h * w), 4)
    
    # === 2. LINE/AXIS DETECTION ===
    lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=80,
                             minLineLength=min(w, h) * 0.1, maxLineGap=10)
    features.line_count = len(lines) if lines is not None else 0
    
    # Perpendicular intersections (chart axes)
    perpendicular_pairs = 0
    if lines is not None and len(lines) > 1:
        angles = []
        for line in lines:
            coords = np.array(line).flatten()
            x1, y1, x2, y2 = int(coords[0]), int(coords[1]), int(coords[2]), int(coords[3])
            angle = np.degrees(np.arctan2(y2 - y1, x2 - x1)) % 180
            angles.append(angle)
        
        for i in range(len(angles)):
            for j in range(i + 1, min(i + 20, len(angles))):
                diff = abs(angles[i] - angles[j])
                if 75 < diff < 105:
                    perpendicular_pairs += 1
    
    features.perpendicular_pairs = perpendicular_pairs
    
    # === 3. COLOR PALETTE DIVERSITY ===
    pixels = hsv.reshape(-1, 3).astype(np.float32)
    if len(pixels) > 10000:
        indices = np.random.choice(len(pixels), 10000, replace=False)
        pixels = pixels[indices]
    
    n_colors = min(15, len(pixels))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, labels, centers = cv2.kmeans(pixels, n_colors, None, criteria, 10, cv2.KMEANS_PP_CENTERS)
    
    color_counts = np.bincount(labels.flatten(), minlength=n_colors)
    total = color_counts.sum()
    probs = color_counts / total
    probs = probs[probs > 0]
    features.color_entropy = round(float(-np.sum(probs * np.log2(probs))), 3)
    features.distinct_colors = int(np.sum(color_counts > total * 0.02))
    
    # === 4. EDGE STRUCTURE ===
    edge_pixels = np.count_nonzero(edges)
    features.edge_density = round(edge_pixels / (h * w), 4)
    
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    long_edges = sum(1 for c in contours if cv2.arcLength(c, False) > min(w, h) * 0.05)
    total_contours = max(len(contours), 1)
    features.long_edge_ratio = round(long_edges / total_contours, 4)
    
    # === 5. BLOB/CONNECTED COMPONENT ANALYSIS ===
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    n_labels, labels_map, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    
    areas = stats[1:, cv2.CC_STAT_AREA] if n_labels > 1 else np.array([])
    if len(areas) > 0:
        features.medium_blobs = int(np.sum((areas > 100) & (areas < (h * w) * 0.3)))
        features.large_blobs = int(np.sum(areas > (h * w) * 0.1))
    
    # === 6. SYMMETRY ===
    mid_h = w // 2
    left = gray[:, :mid_h]
    right = cv2.flip(gray[:, mid_h:mid_h + left.shape[1]], 1)
    if left.shape == right.shape:
        features.symmetry = round(1.0 - (np.mean(np.abs(left.astype(float) - right.astype(float))) / 255.0), 4)
    
    # === 7. BACKGROUND UNIFORMITY ===
    white_mask = cv2.inRange(hsv, np.array([0, 0, 200]), np.array([180, 30, 255]))
    features.white_background = round(np.count_nonzero(white_mask) / (h * w), 4)
    
    # === 8. HUE ANALYSIS ===
    hue_hist = cv2.calcHist([hsv], [0], None, [180], [0, 180])
    hue_hist = hue_hist.flatten() / hue_hist.sum()
    features.hue_concentration = round(float(hue_hist.max()), 4)
    features.hue_diversity = int(np.count_nonzero(hue_hist > 0.01))
    
    # === COMPOSITE SCORE ===
    features.score = compute_score(features)
    features.classification = classify(features.score)
    
    return features


def compute_score(f: ImageFeatures) -> int:
    """Compute a 0-100 graphics-heavy score."""
    score = 50.0  # baseline
    
    # TEXT PENALTY (strong negative signal)
    if f.text_density > 0.4:
        score -= 35
    elif f.text_density > 0.2:
        score -= 25
    elif f.text_density > 0.1:
        score -= 15
    elif f.text_density > 0.05:
        score -= 5
    
    # LINE/AXIS BONUS (chart indicator)
    if f.perpendicular_pairs > 5:
        score += 25
    elif f.perpendicular_pairs > 2:
        score += 15
    elif f.line_count > 10:
        score += 10
    elif f.line_count > 5:
        score += 5
    
    # COLOR DIVERSITY
    if 4 <= f.distinct_colors <= 12:
        score += 10
    elif f.distinct_colors > 12:
        score += 5
    elif f.distinct_colors <= 2:
        score -= 5
    
    # EDGE STRUCTURE
    if f.long_edge_ratio > 0.3:
        score += 10
    elif f.long_edge_ratio > 0.15:
        score += 5
    
    # BLOB ANALYSIS
    if 5 <= f.medium_blobs <= 100:
        score += 10
    elif f.medium_blobs > 100:
        score += 5
    
    # SYMMETRY
    if f.symmetry > 0.6:
        score += 5
    
    # WHITE BACKGROUND
    if f.white_background > 0.5:
        score += 5
    
    # HUE DIVERSITY
    if 3 <= f.hue_diversity <= 10:
        score += 5
    
    return max(0, min(100, round(score)))


def classify(score: int, pass_threshold: int = 70, reject_threshold: int = 40) -> str:
    """Classify a score into pass/borderline/reject."""
    if score >= pass_threshold:
        return "pass"
    elif score >= reject_threshold:
        return "borderline"
    else:
        return "reject"
